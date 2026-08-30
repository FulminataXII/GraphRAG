"""LiteLLMClient against the real LiteLLM gateway. See BLUEPRINT §5.6 / BUILD_ORDER BO-06.

Requires `make up` AND real provider keys in `.env` per MANUAL M-3 (Groq, Google AI Studio,
OpenRouter). Unlike `tests/integration/test_qdrant_store.py` and friends, these do NOT stub
secrets via `tests.unit._settings_helpers.set_required_secrets` — they authenticate with the
REAL `secrets.litellm_virtual_key` from `.env`, since a dummy key cannot pass LiteLLM's own auth.

`test_gateway_fallback` and `test_cost_tracked` call LiteLLM's admin API (master key) to inspect/
mutate live proxy state (temporarily removing a deployment; reading the spend log). Endpoint
shapes (`/model/info`, `/model/delete`, `/model/new`, `/spend/logs`) are LiteLLM's documented
proxy admin API as of this BO but were NOT exercised against a live proxy in this environment
(no docker stack / provider keys available here) — verify against your deployed LiteLLM version
before trusting these in CI.
"""

from __future__ import annotations

from typing import Any

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


def _real_settings() -> Settings:
    """APP_ENV defaults to "local" (config/local.yaml's `llm.gateway_base_url` points at
    localhost). Secrets are read from the real `.env` — NOT stubbed."""
    return Settings()


def _openai_client(settings: Settings) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=settings.llm.gateway_base_url,
        api_key=settings.secrets.litellm_virtual_key.get_secret_value(),
        timeout=settings.llm.request_timeout_s,
        max_retries=0,
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


async def test_gateway_fallback() -> None:
    """Disable the "fast-low-latency" alias's deployments; a `router` call must still succeed,
    served by its configured fallback (`judge-alt-vendor`, per litellm/config.yaml)."""
    settings = _real_settings()
    master_key = settings.secrets.litellm_master_key.get_secret_value()
    base_url = settings.llm.gateway_base_url.rsplit("/v1", 1)[0]
    headers = {"Authorization": f"Bearer {master_key}"}

    async with httpx.AsyncClient(base_url=base_url, timeout=_ADMIN_TIMEOUT) as admin:
        info = await admin.get("/model/info", headers=headers)
        info.raise_for_status()
        primary_deployments = [
            entry for entry in info.json()["data"] if entry["model_name"] == "fast-low-latency"
        ]
        assert primary_deployments, "no 'fast-low-latency' deployments found in /model/info"

        try:
            for entry in primary_deployments:
                resp = await admin.post(
                    "/model/delete", json={"id": entry["model_info"]["id"]}, headers=headers
                )
                resp.raise_for_status()

            openai_client = _openai_client(settings)
            client = _litellm_client(settings, openai_client)
            async with openai_client:
                result = await client.structured(
                    role="router",
                    messages=_route_messages("Who founded Acme Corp?"),
                    schema=RoutePlanOut,
                    max_repairs=1,
                )
            assert result.model_served != "fast-low-latency"
        finally:
            for entry in primary_deployments:
                restore = await admin.post(
                    "/model/new",
                    json={
                        "model_name": entry["model_name"],
                        "litellm_params": entry["litellm_params"],
                    },
                    headers=headers,
                )
                restore.raise_for_status()


async def test_cost_tracked() -> None:
    """Spend LiteLLM records for the app's virtual key tracks within 10% of the app-side token
    counters `LiteLLMClient.structured()` returns on each call."""
    settings = _real_settings()
    openai_client = _openai_client(settings)
    client = _litellm_client(settings, openai_client)

    app_side_tokens = 0
    async with openai_client:
        for _ in range(5):
            result = await client.structured(
                role="router",
                messages=_route_messages("Who founded Acme Corp?"),
                schema=RoutePlanOut,
                max_repairs=1,
            )
            app_side_tokens += result.prompt_tokens + result.completion_tokens

    virtual_key = settings.secrets.litellm_virtual_key.get_secret_value()
    base_url = settings.llm.gateway_base_url.rsplit("/v1", 1)[0]
    master_headers = {
        "Authorization": f"Bearer {settings.secrets.litellm_master_key.get_secret_value()}"
    }

    async with httpx.AsyncClient(base_url=base_url, timeout=_ADMIN_TIMEOUT) as admin:
        spend = await admin.get(
            "/spend/logs", params={"api_key": virtual_key}, headers=master_headers
        )
        spend.raise_for_status()
        rows: list[dict[str, Any]] = spend.json()

    gateway_side_tokens = sum(row.get("total_tokens", 0) for row in rows[-5:])
    assert gateway_side_tokens > 0
    relative_error = abs(gateway_side_tokens - app_side_tokens) / app_side_tokens
    assert relative_error <= 0.10, (
        f"app-side={app_side_tokens} vs gateway-side={gateway_side_tokens} "
        f"(relative error {relative_error:.1%})"
    )
