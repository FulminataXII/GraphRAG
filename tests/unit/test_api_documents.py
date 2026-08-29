"""`POST /v1/documents` unit tests (all-fakes `Container`). See BLUEPRINT §7.1 / BO-05."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from graphrag.apps.api.deps import get_container
from graphrag.apps.api.errors import install_exception_handlers
from graphrag.apps.api.main import Container
from graphrag.apps.api.routers import documents, jobs


@pytest.fixture
def app(container: Container) -> FastAPI:
    fastapi_app = FastAPI()
    install_exception_handlers(fastapi_app)
    fastapi_app.include_router(documents.router)
    fastapi_app.include_router(jobs.router)
    fastapi_app.dependency_overrides[get_container] = lambda: container
    return fastapi_app


@pytest.fixture
async def client(
    app: FastAPI, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.chdir(tmp_path)  # `_upload_storage` writes to a relative `data/uploads/`
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


async def test_ingest_requires_idempotency_key(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/documents", files={"file": ("a.txt", b"hello world", "text/plain")}
    )
    assert response.status_code == 422


async def test_upload_returns_202_with_job_and_doc_id(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/documents",
        files={"file": ("a.txt", b"hello world " * 20, "text/plain")},
        headers={"Idempotency-Key": "key-1"},
    )
    assert response.status_code == 202
    body = response.json()
    assert {"job_id", "doc_id", "correlation_id"} <= body.keys()
    assert body["job_id"] == body["doc_id"]


async def test_duplicate_content_returns_existing_doc_id(
    client: httpx.AsyncClient, container: Container
) -> None:
    """Same bytes under a DIFFERENT Idempotency-Key -> 200 with the original doc_id, no
    second job. Content addressing (sha256), not the header, is what dedupes."""
    payload = b"identical file content " * 20

    first = await client.post(
        "/v1/documents",
        files={"file": ("a.txt", payload, "text/plain")},
        headers={"Idempotency-Key": "key-1"},
    )
    assert first.status_code == 202
    doc_id = first.json()["doc_id"]

    second = await client.post(
        "/v1/documents",
        files={"file": ("a.txt", payload, "text/plain")},
        headers={"Idempotency-Key": "key-2"},
    )
    assert second.status_code == 200
    assert second.json()["doc_id"] == doc_id
    assert len(container.job_queue.enqueued) == 1  # type: ignore[attr-defined]


async def test_upload_exceeding_size_cap_rejected(
    client: httpx.AsyncClient, container: Container
) -> None:
    container.settings = container.settings.model_copy(
        update={"limits": container.settings.limits.model_copy(update={"max_upload_mb": 0})}
    )
    response = await client.post(
        "/v1/documents",
        files={"file": ("a.txt", b"hello world", "text/plain")},
        headers={"Idempotency-Key": "key-1"},
    )
    assert response.status_code == 422


async def test_upload_unsupported_content_type_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/documents",
        files={"file": ("a.bin", b"\x00\x01\x02binary content here", "application/octet-stream")},
        headers={"Idempotency-Key": "key-1"},
    )
    assert response.status_code == 422


async def test_delete_returns_202(client: httpx.AsyncClient) -> None:
    response = await client.delete("/v1/documents/some-doc-id")
    assert response.status_code == 202
    assert response.json()["doc_id"] == "some-doc-id"


async def test_job_status_unknown_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/jobs/does-not-exist")
    assert response.status_code == 404
