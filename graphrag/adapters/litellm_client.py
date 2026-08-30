"""LiteLLMClient — implements `core.ports.LLMClient`. See BLUEPRINT §5.6.

The ONLY component that talks to the LiteLLM gateway, over its OpenAI-compatible HTTP API via
the `openai` SDK (see pyproject.toml's llm-gateway-client comment — there is no `litellm` python
dependency; the gateway is a separate proxy process, reached over HTTP). This process holds
exactly one credential, `secrets.litellm_virtual_key` — individual provider keys live solely in
the LiteLLM proxy's own container environment (litellm/config.yaml, docker-compose.yml).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from openai import APIError, AsyncOpenAI, RateLimitError
from opentelemetry.trace import Status, StatusCode
from pydantic import ValidationError

from graphrag.adapters.telemetry.otel import tracer
from graphrag.core.errors import LLMProviderExhausted, LLMSchemaViolation, RateLimited
from graphrag.core.models import StructuredResult

if TYPE_CHECKING:
    from pydantic import BaseModel

    from graphrag.adapters.telemetry.metrics import Metrics
    from graphrag.config.schema import LLMSection

_log = logging.getLogger(__name__)


def _json_schema_response_format(schema: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema.__name__,
            "schema": schema.model_json_schema(),
            "strict": False,
        },
    }


def _parse_retry_after(headers: Any) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class LiteLLMClient:
    """Implements `LLMClient`.

    Contract (BLUEPRINT §5.6):
        - structured() resolves role -> alias via llm.roles[role].model, then:
            1. Call with response_format json_schema when supported, else json_object.
            2. Parse into `schema`. On a parse/validation failure, re-prompt appending the
               error text as a user message; repeat up to max_repairs.
            3. After max_repairs, raise LLMSchemaViolation with attempts in details.
          Total upstream calls are exactly max_repairs + 1. Never more.
        - Does NOT implement provider retries or fallbacks — LiteLLM owns those (num_retries,
          fallbacks in litellm/config.yaml). The wrapped AsyncOpenAI client is constructed with
          max_retries=0 for the same reason: the SDK's own retry layer would otherwise retry
          transparently underneath us.
        - On 429: reads Retry-After and the x-ratelimit-* headers named in
          llm.adaptive_rate_limit.read_headers, records llm_rate_limited, and raises
          RateLimited with retry_after in details. Static RPM values are never trusted.
        - Emits a span with llm.role, llm.alias, llm.model_served, llm.repair_attempts,
          llm.prompt_tokens, llm.completion_tokens.
        - Records prompts on the span only when observability.traces.record_prompts is true,
          truncated to max_recorded_prompt_chars.
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        llm: LLMSection,
        metrics: Metrics,
        record_prompts: bool,
        max_recorded_prompt_chars: int,
        rate_limit_headers: tuple[str, ...],
    ) -> None:
        self._client = client
        self._llm = llm
        self._metrics = metrics
        self._record_prompts = record_prompts
        self._max_recorded_prompt_chars = max_recorded_prompt_chars
        self._rate_limit_headers = rate_limit_headers

    async def structured[T](
        self,
        *,
        role: str,
        messages: list[dict[str, str]],
        schema: type[T],
        max_repairs: int,
    ) -> StructuredResult[T]:
        role_spec = self._llm.roles[role]
        alias = role_spec.model
        response_format = (
            _json_schema_response_format(schema)
            if self._llm.structured_output.mode == "json_schema"
            else {"type": "json_object"}
        )

        conversation = list(messages)
        started = time.perf_counter()
        completed_repairs = 0
        total_prompt_tokens = 0
        total_completion_tokens = 0

        with tracer().start_as_current_span("LiteLLMClient.structured") as span:
            span.set_attribute("llm.role", role)
            span.set_attribute("llm.alias", alias)
            if self._record_prompts:
                span.set_attribute(
                    "llm.prompt",
                    json.dumps(conversation)[: self._max_recorded_prompt_chars],
                )
            try:
                while True:
                    try:
                        response = await self._client.chat.completions.create(
                            model=alias,
                            messages=conversation,  # type: ignore[arg-type]
                            temperature=role_spec.temperature,
                            max_tokens=role_spec.max_tokens,
                            response_format=response_format,  # type: ignore[arg-type]
                        )
                    except RateLimitError as exc:
                        headers = exc.response.headers
                        retry_after = _parse_retry_after(headers)
                        self._metrics.llm_rate_limited.add(1, {"llm_role": role})
                        raise RateLimited(
                            f"LiteLLM gateway rate limited role={role}",
                            retry_after=retry_after,
                            details={
                                "role": role,
                                "alias": alias,
                                **{
                                    h: headers.get(h)
                                    for h in self._rate_limit_headers
                                    if headers.get(h) is not None
                                },
                            },
                        ) from exc
                    except APIError as exc:
                        raise LLMProviderExhausted(
                            f"LiteLLM gateway call failed for role={role}: {exc}",
                            details={"role": role, "alias": alias},
                        ) from exc

                    # BLUEPRINT §3.2: prompt_tokens/completion_tokens are the sum across every
                    # upstream call this structured() invocation makes, including rejected
                    # repair attempts — a provider bills for a malformed response exactly as
                    # for a good one, so counting only the final attempt understates spend.
                    usage = response.usage
                    total_prompt_tokens += usage.prompt_tokens if usage else 0
                    total_completion_tokens += usage.completion_tokens if usage else 0

                    raw_content = response.choices[0].message.content or ""
                    try:
                        parsed = json.loads(raw_content)
                        value = schema.model_validate(parsed)
                    except (json.JSONDecodeError, ValidationError) as exc:
                        if completed_repairs >= max_repairs:
                            raise LLMSchemaViolation(
                                f"role={role} exceeded max_repairs={max_repairs}",
                                details={"role": role, "attempts": completed_repairs + 1},
                            ) from exc
                        conversation = [
                            *conversation,
                            {"role": "assistant", "content": raw_content},
                            {
                                "role": "user",
                                "content": f"That response was invalid: {exc}. "
                                "Reply again with ONLY corrected JSON matching the schema.",
                            },
                        ]
                        completed_repairs += 1
                        continue

                    latency_ms = int((time.perf_counter() - started) * 1000)

                    span.set_attribute("llm.model_served", response.model)
                    span.set_attribute("llm.repair_attempts", completed_repairs)
                    span.set_attribute("llm.prompt_tokens", total_prompt_tokens)
                    span.set_attribute("llm.completion_tokens", total_completion_tokens)

                    self._metrics.repair_attempts.record(completed_repairs, {"llm_role": role})
                    self._metrics.llm_tokens.add(
                        total_prompt_tokens, {"llm_role": role, "direction": "prompt"}
                    )
                    self._metrics.llm_tokens.add(
                        total_completion_tokens, {"llm_role": role, "direction": "completion"}
                    )

                    return StructuredResult(
                        value=value,
                        model_served=response.model,
                        repair_attempts=completed_repairs,
                        prompt_tokens=total_prompt_tokens,
                        completion_tokens=total_completion_tokens,
                        latency_ms=latency_ms,
                    )
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise

    async def stream_text(self, *, role: str, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        role_spec = self._llm.roles[role]
        try:
            stream = await self._client.chat.completions.create(
                model=role_spec.model,
                messages=messages,  # type: ignore[arg-type]
                temperature=role_spec.temperature,
                max_tokens=role_spec.max_tokens,
                stream=True,
            )
        except APIError as exc:
            raise LLMProviderExhausted(
                f"LiteLLM gateway stream failed for role={role}: {exc}", details={"role": role}
            ) from exc
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    async def health(self) -> bool:
        try:
            await self._client.models.list()
            return True
        except Exception:
            return False
