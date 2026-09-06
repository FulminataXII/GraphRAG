"""`DELETE /v1/documents/{id}` against a real Redis-backed `ArqJobQueue`. See BLUEPRINT §7.1.

The unit-level `tests/unit/test_api_documents.py` already covers the router's status codes and
branching against an all-fakes `Container` — this file's job is narrower: prove the 202 really
corresponds to a job landing in REAL Redis, not just a fake recording a call.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

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
from graphrag.core.errors import ConflictError
from graphrag.core.events import DELETE_DOCUMENT, DeleteDocumentPayload, JobEnvelope
from tests.factories import make_metrics
from tests.fakes import FakeCache, FakeDocumentLedger, FakeSourceRegistry
from tests.integration import namespaces as ns
from tests.unit._settings_helpers import set_required_secrets

pytestmark = pytest.mark.integration

# NOT db 0: these tests enqueue real jobs, and on the production queue the running
# `graphrag-worker-1` would pick them up. See tests/integration/namespaces.py.
_REDIS_URL = ns.REDIS_URL


@pytest.fixture
async def arq_pool() -> AsyncIterator[ArqRedis]:
    pool = await create_pool(RedisSettings.from_dsn(_REDIS_URL))
    yield pool
    await pool.aclose()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    set_required_secrets(monkeypatch)
    return ns.namespaced(Settings())


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


async def test_second_delete_for_the_same_doc_id_conflicts(
    client: httpx.AsyncClient, container: Container
) -> None:
    """Re-issuing a DELETE while arq still holds that job_id must not return a false 202.

    This is the API-side face of the enqueue bug: arq refuses a second job under an id it
    already holds and returns None, which `ArqJobQueue.enqueue` used to paper over by returning
    the caller's own id. The endpoint answered 202 — "accepted, deletion queued" — for a
    deletion that was never queued at all. 409 is the truthful answer.
    """
    doc_id = f"doc-{uuid.uuid4().hex}"
    assert (await client.delete(f"/v1/documents/{doc_id}")).status_code == 202

    response = await client.delete(f"/v1/documents/{doc_id}")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "CONFLICT"
    # The remedy has to travel with the error; nobody rediscovers keep_result_s from a 409.
    assert "--force" in error["message"]


async def test_drop_job_makes_a_job_id_reusable(container: Container) -> None:
    """`graphrag ingest --force` depends on this against real arq, not a fake.

    arq blocks a re-enqueue while EITHER `arq:job:<id>` or `arq:result:<id>` exists, and it
    keeps a finished job's result for `keep_result` (3600s by default) — so without clearing
    those keys a forced re-ingest inside the hour is silently dropped.
    """
    doc_id = f"doc-{uuid.uuid4().hex}"
    envelope = JobEnvelope(
        correlation_id="cid-force",
        otel={},
        enqueued_at=datetime.now(UTC),
        payload=DeleteDocumentPayload(doc_id=doc_id),
    )
    await container.job_queue.enqueue(DELETE_DOCUMENT, envelope, job_id=doc_id)

    with pytest.raises(ConflictError):
        await container.job_queue.enqueue(DELETE_DOCUMENT, envelope, job_id=doc_id)

    await container.job_queue.drop_job(doc_id)

    # Reusable again -- which is exactly what --force needs.
    assert await container.job_queue.enqueue(DELETE_DOCUMENT, envelope, job_id=doc_id) == doc_id
