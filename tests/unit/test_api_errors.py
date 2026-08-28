"""`apps/api/errors.py` unit tests. See BLUEPRINT §7.1.

A minimal FastAPI app wired only with `install_exception_handlers` and two deliberately-failing
routes — no Container, no real backend, so this stays `unit`.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graphrag.apps.api.errors import install_exception_handlers
from graphrag.core.errors import NotFound, RateLimited


def _app() -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)

    @app.middleware("http")
    async def _fake_correlation_id(request, call_next):  # type: ignore[no-untyped-def]
        request.scope["correlation_id"] = "test-correlation-id"
        return await call_next(request)

    @app.get("/boom-app-error")
    async def _boom_app_error() -> None:
        raise NotFound("document not found", details={"doc_id": "abc"})

    @app.get("/boom-rate-limited")
    async def _boom_rate_limited() -> None:
        raise RateLimited("slow down", retry_after=12.5)

    @app.get("/boom-unhandled")
    async def _boom_unhandled() -> None:
        raise RuntimeError("something truly unexpected: password=hunter2")

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app(), raise_server_exceptions=False)


def test_error_envelope_shape(client: TestClient) -> None:
    response = client.get("/boom-app-error")
    assert response.status_code == 404
    body = response.json()["error"]
    assert body["code"] == "NOT_FOUND"
    assert body["message"] == "document not found"
    assert body["correlation_id"] == "test-correlation-id"
    assert body["retryable"] is False
    assert body["details"] == {"doc_id": "abc"}
    assert "trace_id" in body


def test_rate_limited_includes_retry_after(client: TestClient) -> None:
    response = client.get("/boom-rate-limited")
    assert response.status_code == 429
    assert response.json()["error"]["details"]["retry_after"] == 12.5


def test_500_leaks_no_traceback(client: TestClient) -> None:
    response = client.get("/boom-unhandled")
    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "INTERNAL_ERROR"
    raw = response.text
    assert "Traceback" not in raw
    assert "RuntimeError" not in raw
    assert "hunter2" not in raw
    assert body["correlation_id"] == "test-correlation-id"
