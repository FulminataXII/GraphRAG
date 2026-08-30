"""`DELETE /v1/documents/{id}` against a real Redis-backed `ArqJobQueue`. See BLUEPRINT §7.1.

The unit-level `tests/unit/test_api_documents.py` already covers the router's status codes and
branching against an all-fakes `Container` — this file's job is narrower: prove the 202 really
corresponds to a job landing in REAL Redis, not just a fake recording a call.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from arq.connections import ArqRedis, RedisSettings, create_pool
from fastapi import FastAPI

from graphrag.adapters.arq_queue import ArqJobQueue
from graphrag.apps.api.deps import get_container
from graphrag.apps.api.errors import install_exception_handlers
from graphrag.apps.api.main import Container, ReadyzProber
from graphrag.apps.api.routers import documents
from graphrag.config.settings import Settings
from tests.factories import make_metrics
from tests.fakes import FakeCache, FakeDocumentLedger, FakeSourceRegistry
from tests.unit._settings_helpers import set_required_secrets

pytestmark = pytest.mark.integration

_REDIS_URL = "redis://localhost:6379/0"


@pytest.fixture
async def arq_pool() -> AsyncIterator[ArqRedis]:
    pool = await create_pool(RedisSettings.from_dsn(_REDIS_URL))
    yield pool
    await pool.aclose()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    set_required_secrets(monkeypatch)
    return Settings()


@pytest.fixture
def container(settings: Settings, arq_pool: ArqRedis) -> Container:
    return Container(
        settings=settings,
        ledger=FakeDocumentLedger(),
        sources=FakeSourceRegistry(),
        cache=FakeCache(),
        job_queue=ArqJobQueue(arq_pool),
        readyz_prober=ReadyzProber({}, cache_s=5, timeout_s=1),
        metrics=make_metrics()[0],
    )


@pytest.fixture
async def client(container: Container) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(documents.router)
    app.dependency_overrides[get_container] = lambda: container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


async def test_delete_returns_202(client: httpx.AsyncClient, container: Container) -> None:
    doc_id = f"doc-{uuid.uuid4().hex}"
    response = await client.delete(f"/v1/documents/{doc_id}")

    assert response.status_code == 202
    body = response.json()
    assert body["doc_id"] == doc_id
    assert body["job_id"] == doc_id

    # a real job actually landed in Redis, not just a fake recording a call
    job_status = await container.job_queue.status(doc_id)
    assert job_status.state != "not_found"
