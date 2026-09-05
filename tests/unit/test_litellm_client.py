"""`LiteLLMClient` unit tests. See BLUEPRINT §5.6.

Uses a hand-rolled fake OpenAI SDK client (BLUEPRINT §9: "NO mocks, NO MagicMock") that only
implements the `chat.completions.create` / `models.list` shape `LiteLLMClient` actually calls.
Responses are REAL `openai` SDK pydantic types (`ChatCompletion`, `RateLimitError`, ...)
constructed directly with fake data — never a mock object standing in for them.
"""

from __future__ import annotations

from typing import Any

import httpx2
import pytest
from openai import InternalServerError, RateLimitError
from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.completion_usage import CompletionUsage
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from graphrag.adapters.litellm_client import LiteLLMClient
from graphrag.adapters.telemetry.metrics import Metrics
from graphrag.config.schema import LLMSection
from graphrag.config.settings import Settings
from graphrag.core.errors import LLMProviderExhausted, LLMSchemaViolation, RateLimited
from graphrag.services.orchestration.schemas import RoutePlanOut


def _completion(
    content: str,
    *,
    model: str = "fast-low-latency",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> ChatCompletion:
    return ChatCompletion(
        id="chatcmpl-test",
        choices=[
            Choice(
                finish_reason="stop",
                index=0,
                message=ChatCompletionMessage(role="assistant", content=content),
            )
        ],
        created=0,
        model=model,
        object="chat.completion",
        usage=CompletionUsage(
            completion_tokens=completion_tokens,
            prompt_tokens=prompt_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


_VALID_ROUTE_PLAN_JSON = (
    '{"strategy": "vector", "seed_entities": [], "hops": 1, "sub_queries": [], '
    '"rationale": "no entities mentioned"}'
)


def _rate_limit_error(
    *, retry_after: str = "7", extra_headers: dict[str, str] | None = None
) -> RateLimitError:
    headers = {"retry-after": retry_after, **(extra_headers or {})}
    resp = httpx2.Response(
        429, headers=headers, request=httpx2.Request("POST", "http://litellm/v1/chat/completions")
    )
    return RateLimitError("rate limited", response=resp, body=None)


def _server_error() -> InternalServerError:
    resp = httpx2.Response(
        500, request=httpx2.Request("POST", "http://litellm/v1/chat/completions")
    )
    return InternalServerError("boom", response=resp, body=None)


class _FakeCompletions:
    """Queued responses; each queued item is a `ChatCompletion` or an `Exception` to raise."""

    def __init__(self, *items: Any) -> None:
        self._queue: list[Any] = list(items)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> ChatCompletion:
        self.calls.append(kwargs)
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeModels:
    async def list(self) -> list[Any]:
        return []


class _FakeAsyncOpenAI:
    def __init__(self, *items: Any) -> None:
        self.completions = _FakeCompletions(*items)
        self.chat = _FakeChat(self.completions)
        self.models = _FakeModels()


def _metrics() -> tuple[Metrics, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    return Metrics(provider.get_meter("test")), reader


def _counter_total(reader: InMemoryMetricReader, metric_name: str) -> float:
    data = reader.get_metrics_data()
    if data is None:
        return 0.0
    total = 0.0
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != metric_name:
                    continue
                for point in metric.data.data_points:
                    total += point.value
    return total


def _client(
    fake: _FakeAsyncOpenAI, llm: LLMSection, *, metrics: Metrics | None = None
) -> LiteLLMClient:
    return LiteLLMClient(
        fake,  # type: ignore[arg-type]
        llm=llm,
        metrics=metrics or _metrics()[0],
        record_prompts=False,
        max_recorded_prompt_chars=2000,
        rate_limit_headers=("retry-after", "x-ratelimit-remaining-requests"),
    )


async def test_structured_valid_first_try(settings: Settings) -> None:
    fake = _FakeAsyncOpenAI(_completion(_VALID_ROUTE_PLAN_JSON))
    client = _client(fake, settings.llm)

    result = await client.structured(
        role="router",
        messages=[{"role": "user", "content": "hi"}],
        schema=RoutePlanOut,
        max_repairs=2,
    )

    assert len(fake.completions.calls) == 1
    assert result.repair_attempts == 0
    assert result.value.strategy == "vector"


async def test_structured_repairs_then_succeeds(settings: Settings) -> None:
    fake = _FakeAsyncOpenAI(
        _completion("not json", prompt_tokens=10, completion_tokens=5),
        _completion(_VALID_ROUTE_PLAN_JSON, prompt_tokens=20, completion_tokens=8),
    )
    client = _client(fake, settings.llm)

    result = await client.structured(
        role="router",
        messages=[{"role": "user", "content": "hi"}],
        schema=RoutePlanOut,
        max_repairs=2,
    )

    assert len(fake.completions.calls) == 2
    assert result.repair_attempts == 1
    # BLUEPRINT §3.2: prompt_tokens/completion_tokens sum EVERY upstream call, including the
    # rejected repair attempt — a provider bills for a malformed response like any other.
    assert result.prompt_tokens == 10 + 20
    assert result.completion_tokens == 5 + 8


async def test_structured_raises_after_max_repairs(settings: Settings) -> None:
    max_repairs = 2
    fake = _FakeAsyncOpenAI(*[_completion("not json") for _ in range(max_repairs + 1)])
    client = _client(fake, settings.llm)

    with pytest.raises(LLMSchemaViolation):
        await client.structured(
            role="router",
            messages=[{"role": "user", "content": "hi"}],
            schema=RoutePlanOut,
            max_repairs=max_repairs,
        )

    assert len(fake.completions.calls) == max_repairs + 1


async def test_repair_prompt_includes_validation_error(settings: Settings) -> None:
    fake = _FakeAsyncOpenAI(_completion("not json"), _completion(_VALID_ROUTE_PLAN_JSON))
    client = _client(fake, settings.llm)

    await client.structured(
        role="router",
        messages=[{"role": "user", "content": "hi"}],
        schema=RoutePlanOut,
        max_repairs=2,
    )

    second_call_messages = fake.completions.calls[1]["messages"]
    repair_message = second_call_messages[-1]
    assert repair_message["role"] == "user"
    assert "invalid" in repair_message["content"].lower()
    assert "not json" not in repair_message["content"]


async def test_429_reads_retry_after(settings: Settings) -> None:
    fake = _FakeAsyncOpenAI(_rate_limit_error(retry_after="7"))
    client = _client(fake, settings.llm)

    with pytest.raises(RateLimited) as exc_info:
        await client.structured(
            role="router",
            messages=[{"role": "user", "content": "hi"}],
            schema=RoutePlanOut,
            max_repairs=2,
        )

    assert exc_info.value.retry_after == 7.0


async def test_429_increments_rate_limited_metric(settings: Settings) -> None:
    metrics, reader = _metrics()
    fake = _FakeAsyncOpenAI(_rate_limit_error())
    client = _client(fake, settings.llm, metrics=metrics)

    with pytest.raises(RateLimited):
        await client.structured(
            role="router",
            messages=[{"role": "user", "content": "hi"}],
            schema=RoutePlanOut,
            max_repairs=2,
        )

    assert _counter_total(reader, "graphrag.llm.rate_limited") == 1


async def test_client_does_not_retry_provider_errors(settings: Settings) -> None:
    fake = _FakeAsyncOpenAI(_server_error())
    client = _client(fake, settings.llm)

    with pytest.raises(LLMProviderExhausted):
        await client.structured(
            role="router",
            messages=[{"role": "user", "content": "hi"}],
            schema=RoutePlanOut,
            max_repairs=2,
        )

    assert len(fake.completions.calls) == 1


async def test_per_role_timeout_is_sent_on_every_upstream_call(settings: Settings) -> None:
    """`router` carries its own `timeout_s`, so every call it makes -- the repair attempts
    included -- must be bounded by that, not by the global `request_timeout_s` the shared
    AsyncOpenAI client was constructed with."""
    role_timeout = settings.llm.roles["router"].timeout_s
    assert role_timeout is not None and role_timeout != settings.llm.request_timeout_s
    fake = _FakeAsyncOpenAI(_completion("not json"), _completion(_VALID_ROUTE_PLAN_JSON))
    client = _client(fake, settings.llm)

    await client.structured(
        role="router",
        messages=[{"role": "user", "content": "hi"}],
        schema=RoutePlanOut,
        max_repairs=2,
    )

    assert len(fake.completions.calls) == 2
    assert [call["timeout"] for call in fake.completions.calls] == [role_timeout, role_timeout]


async def test_role_without_timeout_falls_back_to_request_timeout(settings: Settings) -> None:
    """The field is optional so existing behaviour is preserved: a role that names no
    `timeout_s` is still bounded by `llm.request_timeout_s`."""
    llm = settings.llm.model_copy(
        update={
            "roles": {
                **settings.llm.roles,
                "router": settings.llm.roles["router"].model_copy(update={"timeout_s": None}),
            }
        }
    )
    fake = _FakeAsyncOpenAI(_completion(_VALID_ROUTE_PLAN_JSON))
    client = _client(fake, llm)

    await client.structured(
        role="router",
        messages=[{"role": "user", "content": "hi"}],
        schema=RoutePlanOut,
        max_repairs=2,
    )

    assert fake.completions.calls[0]["timeout"] == settings.llm.request_timeout_s
