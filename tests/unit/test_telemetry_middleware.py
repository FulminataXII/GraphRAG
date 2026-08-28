"""BO-02: telemetry/middleware.py. See BUILD_ORDER.md.

Uses a minimal hand-rolled ASGI app (not FastAPI/Starlette — apps/api doesn't exist until
BO-03) so `CorrelationIdMiddleware` and `AccessLogMiddleware` are exercised at the raw ASGI
level they're actually implemented against. `httpx.ASGITransport` only supports the async
client, so these are async tests (asyncio_mode="auto" in pytest config — no decorator needed).
"""

from __future__ import annotations

import json

import httpx
import structlog

from graphrag.adapters.telemetry.logging import configure_logging
from graphrag.adapters.telemetry.middleware import AccessLogMiddleware, CorrelationIdMiddleware
from graphrag.config.settings import Settings


async def _echo_correlation_app(scope, receive, send) -> None:
    """Binds an extra contextvar field INSIDE the endpoint, then echoes correlation_id back."""
    structlog.contextvars.bind_contextvars(endpoint_field="bound-in-endpoint")
    body = json.dumps({"correlation_id": scope.get("correlation_id")}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


async def _error_app(scope, receive, send) -> None:
    await send({"type": "http.response.start", "status": 500, "headers": []})
    await send({"type": "http.response.body", "body": b"boom"})


def _client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_correlation_id_echoed_when_supplied(settings: Settings) -> None:
    app = CorrelationIdMiddleware(_echo_correlation_app, header_name="X-Correlation-ID")
    async with _client_for(app) as client:
        response = await client.get("/", headers={"X-Correlation-ID": "supplied-cid-123"})

    assert response.headers["x-correlation-id"] == "supplied-cid-123"
    assert response.json()["correlation_id"] == "supplied-cid-123"


async def test_correlation_id_generated_when_absent(settings: Settings) -> None:
    app = CorrelationIdMiddleware(_echo_correlation_app)
    async with _client_for(app) as client:
        response = await client.get("/")

    header_value = response.headers["x-correlation-id"]
    assert header_value
    assert response.json()["correlation_id"] == header_value


async def test_correlation_id_present_on_error_response(settings: Settings) -> None:
    app = CorrelationIdMiddleware(_error_app)
    async with _client_for(app) as client:
        response = await client.get("/", headers={"X-Correlation-ID": "err-cid"})

    assert response.status_code == 500
    assert response.headers["x-correlation-id"] == "err-cid"


async def test_correlation_id_survives_endpoint_contextvars(settings: Settings, capsys) -> None:
    """The pure-ASGI-vs-BaseHTTPMiddleware test: a field bound INSIDE the endpoint must still
    be visible to AccessLogMiddleware's log call, which runs AFTER `await self._app(...)`
    returns. BaseHTTPMiddleware fails this because it runs the endpoint in a separate task.

    CorrelationIdMiddleware is the OUTER layer: its `finally: clear_request_context()` must not
    fire until AccessLogMiddleware (inner, closer to the app) has already logged — so the access
    log line can see both `correlation_id` and whatever the endpoint bound.
    """
    configure_logging(settings, service_role="api")

    app = CorrelationIdMiddleware(AccessLogMiddleware(_echo_correlation_app))
    async with _client_for(app) as client:
        await client.get("/", headers={"X-Correlation-ID": "ctx-cid"})

    # Stdlib loggers other than ours (httpx's own request-log line, e.g.) also flow through the
    # root handler configure_logging() installs, but as plain text, not JSON — filter to just
    # the JSON lines our own structlog calls produced.
    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip().startswith("{")
    ]
    (access_log,) = [line for line in lines if line["event"] == "http_request"]
    assert access_log["correlation_id"] == "ctx-cid"
    assert access_log["endpoint_field"] == "bound-in-endpoint"


async def test_access_log_skips_health_paths(settings: Settings, capsys) -> None:
    configure_logging(settings, service_role="api")

    async def health_app(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    app = AccessLogMiddleware(health_app)
    async with _client_for(app) as client:
        await client.get("/healthz")

    # Stdlib loggers other than ours (httpx's own request-log line, e.g.) also flow through the
    # root handler configure_logging() installs, but as plain text, not JSON — filter to just
    # the JSON lines our own structlog calls produced.
    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip().startswith("{")
    ]
    assert not any(line.get("event") == "http_request" for line in lines)
