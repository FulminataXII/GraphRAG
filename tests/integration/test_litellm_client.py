"""LiteLLMClient against the real LiteLLM gateway. See BLUEPRINT §5.6 / BUILD_ORDER BO-06.

Requires `make up` AND real provider keys in `.env` per MANUAL M-3 (Groq, Google AI Studio,
OpenRouter). Unlike `tests/integration/test_qdrant_store.py` and friends, these do NOT stub
secrets via `tests.unit._settings_helpers.set_required_secrets` — they authenticate with the
REAL `secrets.litellm_virtual_key` from `.env`, since a dummy key cannot pass LiteLLM's own auth.

`test_key_rotation_spreads_load`, `test_gateway_fallback`, and `test_cost_tracked` all burn real
provider quota (marked `llm_quota`, on top of the module's `integration` mark).

`test_gateway_fallback` is config-driven, not admin-API-driven: models come from
litellm/config.yaml (STORE_MODEL_IN_DB is off), so `/model/delete` and `/model/new` 400/500
against it and mutating live proxy state through them is unsafe besides (a failed restore would
break the gateway for every later test). Instead litellm/config.yaml defines a dedicated
`test-fallback-chain` alias whose only deployment has an unreachable `api_base`, with a fallback
to the real `fast-low-latency` alias, and the test reads LiteLLM's own routing headers off a
direct gateway call to prove which alias served it — no admin API, no mutation, no cleanup.

`test_cost_tracked` still reads `/spend/logs` (master key, read-only). Its response shape (a
JSON list of dicts with a `total_tokens` key per row) was confirmed against a live proxy for this
BO. So was its correlation mechanism — checked in the order asked, both against the live proxy,
not assumed:
  - `x-litellm-trace-id` (a client-set request header, on this pinned LiteLLM version) round-trips
    verbatim into each spend-log row's `session_id`. Confirmed exact and clock-independent: no
    reliance on row order or a wall-clock window. This is what the test uses.
  - `x-litellm-call-id` was checked and does NOT correlate to a row's `request_id` here — the
    header's value never matched; only the completion response's own `id` field does (which
    `structured()` does not surface). Noted in case a future LiteLLM version changes this.
  - `/spend/logs` rows are also NOT chronologically ordered (observed: adjacent rows hours apart)
    and LiteLLM flushes them on a background interval, observed ~10s on this proxy — neither
    matters now that correlation is by trace id rather than by time, but re-verify both if you
    upgrade the LiteLLM image and this ever falls back to a time window.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4

import httpx
import pytest
from openai import AsyncOpenAI

from graphrag.adapters.litellm_client import LiteLLMClient
from graphrag.adapters.telemetry.metrics import Metrics
from graphrag.adapters.telemetry.otel import meter
from graphrag.config.settings import Settings
from graphrag.services.orchestration.schemas import RoutePlanOut

pytestmark = pytest.mark.integration

_ADMIN_TIMEOUT = 15.0
_SPEND_LOG_POLL_TIMEOUT_S = 40.0
_SPEND_LOG_POLL_INTERVAL_S = 3.0


def _real_settings() -> Settings:
    """APP_ENV defaults to "local" (config/local.yaml's `llm.gateway_base_url` points at
    localhost). Secrets are read from the real `.env` — NOT stubbed."""
    return Settings()


def _openai_client(
    settings: Settings, *, extra_headers: dict[str, str] | None = None
) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=settings.llm.gateway_base_url,
        api_key=settings.secrets.litellm_virtual_key.get_secret_value(),
        timeout=settings.llm.request_timeout_s,
        max_retries=0,
        default_headers=extra_headers,
    )


def _litellm_client(settings: Settings, openai_client: AsyncOpenAI) -> LiteLLMClient:
    return LiteLLMClient(
        openai_client,
        llm=settings.llm,
        metrics=Metrics(meter()),
        record_prompts=False,
        max_recorded_prompt_chars=2000,
        rate_limit_headers=tuple(settings.llm.adaptive_rate_limit.read_headers),
    )


def _route_messages(question: str) -> list[dict[str, str]]:
    from graphrag.services.orchestration.prompts import render

    return [{"role": "user", "content": render("route_plan.j2", question=question)}]


@pytest.mark.llm_quota
async def test_key_rotation_spreads_load() -> None:
    """50 calls against the "fast-low-latency" alias (2 deployments, 2 Groq keys — MANUAL M-3)
    must hit both `model_id`s, not just the first one LiteLLM happens to pick."""
    settings = _real_settings()
    openai_client = _openai_client(settings)
    role_spec = settings.llm.roles["router"]
    served_model_ids: set[str] = set()

    async with openai_client:
        for _ in range(50):
            raw = await openai_client.chat.completions.with_raw_response.create(
                model=role_spec.model,
                messages=_route_messages("Who founded Acme Corp?"),
                temperature=role_spec.temperature,
                max_tokens=role_spec.max_tokens,
            )
            model_id = raw.headers.get("x-litellm-model-id")
            if model_id:
                served_model_ids.add(model_id)

    assert len(served_model_ids) >= 2, (
        f"expected rotation across >=2 deployments, only saw {served_model_ids}"
    )


@pytest.mark.llm_quota
async def test_gateway_fallback() -> None:
    """The `test_fallback_chain` role (base.yaml) resolves to litellm/config.yaml's
    `test-fallback-chain` alias, whose only deployment points at `http://127.0.0.1:9` (nothing
    listens there — always unreachable) with a configured fallback to the real `fast-low-latency`
    alias.

    This calls the gateway directly (mirroring `test_key_rotation_spreads_load`) rather than
    through `LiteLLMClient.structured()`: `structured()` forces `response_format: json_schema`,
    and — confirmed against the running proxy, not assumed — Groq rejects a schema-constrained
    request outright (400 `json_validate_failed`) once `max_tokens: 1` makes the schema
    unsatisfiable, which happens identically on every alias in the fallback chain and so surfaces
    as an unhelpful `LLMProviderExhausted` regardless of whether the fallback engaged. Without a
    schema, LiteLLM just truncates the completion — and its own `x-litellm-model-group` /
    `x-litellm-attempted-fallbacks` response headers say plainly which alias served the request,
    proving the fallback (not the dead primary) did, without touching LiteLLM's admin API or
    mutating live proxy state.
    """
    settings = _real_settings()
    openai_client = _openai_client(settings)
    role_spec = settings.llm.roles["test_fallback_chain"]

    async with openai_client:
        raw = await openai_client.chat.completions.with_raw_response.create(
            model=role_spec.model,
            messages=_route_messages("Who founded Acme Corp?"),
            temperature=role_spec.temperature,
            max_tokens=role_spec.max_tokens,
        )

    assert raw.headers.get("x-litellm-attempted-fallbacks") not in (None, "0"), (
        "expected the always-unreachable primary to trigger a fallback attempt"
    )
    assert raw.headers.get("x-litellm-model-group") == "fast-low-latency", (
        f"expected the configured fallback alias to serve the request, got "
        f"{raw.headers.get('x-litellm-model-group')!r}"
    )


@pytest.mark.llm_quota
async def test_cost_tracked() -> None:
    """Spend LiteLLM records for the app's virtual key tracks within 10% of the app-side token
    counters `LiteLLMClient.structured()` returns on each call.

    Correlated by `x-litellm-trace-id`, set once as a default header on this test's own client
    and round-tripped by LiteLLM into every spend-log row's `session_id` — exact and
    clock-independent (see module docstring for what was checked and ruled out).

    `max_repairs` stays at its configured value (1): `structured()` (BLUEPRINT §3.2) now sums
    `prompt_tokens`/`completion_tokens` across every attempt, including rejected repairs, so a
    repair is no longer a blind spot to force away. Per BLUEPRINT §5.6, `structured()` makes
    exactly `repair_attempts + 1` upstream calls — never more — so that sum, over every call this
    test makes, is the exact number of gateway rows expected. The poll target is computed from
    the real `repair_attempts` LiteLLM's router role happened to need, not a hardcoded guess.
    """
    settings = _real_settings()
    trace_id = f"test-cost-tracked-{uuid4()}"
    openai_client = _openai_client(settings, extra_headers={"x-litellm-trace-id": trace_id})
    client = _litellm_client(settings, openai_client)

    n_calls = 3
    results = []
    async with openai_client:
        for _ in range(n_calls):
            result = await client.structured(
                role="router",
                messages=_route_messages("Who founded Acme Corp?"),
                schema=RoutePlanOut,
                max_repairs=1,
            )
            results.append(result)

    app_side_tokens = sum(r.prompt_tokens + r.completion_tokens for r in results)
    expected_rows = sum(r.repair_attempts + 1 for r in results)

    virtual_key = settings.secrets.litellm_virtual_key.get_secret_value()
    base_url = settings.llm.gateway_base_url.rsplit("/v1", 1)[0]
    master_headers = {
        "Authorization": f"Bearer {settings.secrets.litellm_master_key.get_secret_value()}"
    }

    # Poll to an exact count rather than reading once (LiteLLM flushes spend logs on a background
    # interval, observed ~10s on this proxy) or trusting `>=` (a stray row under the same trace
    # id — an internal retry/fallback attempt — would make the comparison run on the wrong set).
    matched_rows: list[dict[str, Any]] = []
    success_rows: list[dict[str, Any]] = []
    deadline = time.monotonic() + _SPEND_LOG_POLL_TIMEOUT_S
    async with httpx.AsyncClient(base_url=base_url, timeout=_ADMIN_TIMEOUT) as admin:
        while True:
            spend = await admin.get(
                "/spend/logs", params={"api_key": virtual_key}, headers=master_headers
            )
            spend.raise_for_status()
            matched_rows = [row for row in spend.json() if row.get("session_id") == trace_id]
            success_rows = [row for row in matched_rows if row.get("status") == "success"]
            if len(success_rows) >= expected_rows or time.monotonic() >= deadline:
                break
            await asyncio.sleep(_SPEND_LOG_POLL_INTERVAL_S)

    other_rows = [row for row in matched_rows if row.get("status") != "success"]

    def _describe(row: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
        return (
            row.get("model"),
            row.get("status"),
            row.get("total_tokens"),
            row.get("metadata", {}).get("attempted_fallbacks"),
        )

    assert len(success_rows) == expected_rows, (
        f"expected exactly {expected_rows} successful spend-log row(s) for "
        f"x-litellm-trace-id={trace_id!r} (sum of repair_attempts+1 across {n_calls} calls: "
        f"{[r.repair_attempts for r in results]}), got {len(success_rows)} after "
        f"{_SPEND_LOG_POLL_TIMEOUT_S:.0f}s. other-status rows in the same correlated set: "
        f"{[_describe(row) for row in other_rows]}"
    )
    # A non-success row under our own trace id means some attempt was retried or fell back at the
    # gateway level beneath structured() — app-side (which only sees the attempts structured()
    # itself made) and gateway-side (every HTTP attempt LiteLLM made) would then no longer
    # describe the same population, even though the token SUM assertion below might still pass
    # by coincidence (retried attempts are typically logged with 0 tokens).
    assert not other_rows, (
        f"correlated set for trace_id={trace_id!r} contains non-success row(s) — app-side and "
        f"gateway-side may not describe the same population: {[_describe(row) for row in other_rows]}"
    )

    gateway_side_tokens = sum(row.get("total_tokens", 0) for row in success_rows)
    assert gateway_side_tokens > 0
    relative_error = abs(gateway_side_tokens - app_side_tokens) / app_side_tokens
    assert relative_error <= 0.10, (
        f"app-side={app_side_tokens} vs gateway-side={gateway_side_tokens} "
        f"(relative error {relative_error:.1%}, {len(success_rows)} rows)"
    )
