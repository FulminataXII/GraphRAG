"""`IngestionService`/`ProjectionService`/`DeletionService` against real Postgres + Qdrant.

See BLUEPRINT §6.1 / BUILD_ORDER BO-05. Uses `FakeGraphStore` (Neo4jGraphStore doesn't exist
until BO-08 — `graph_store: GraphStore | None` is how `Container` already tolerates that, see
`services/ingestion/service.py`'s module docstring) and `FakeJobQueue`/`FakeEmbedder` (their own
correctness isn't under test here; BO-04 covers embeddings, `test_trace_propagation.py` and
`tests/unit/test_arq_queue.py` cover the real queue).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from collections.abc import AsyncIterator

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine

from graphrag.adapters.postgres.ledger import PostgresDocumentLedger
from graphrag.adapters.postgres.sources import PostgresSourceRegistry
from graphrag.adapters.qdrant_store import QdrantVectorStore
from graphrag.config.settings import Settings
from graphrag.core.ids import chunk_id
from graphrag.core.models import DocumentStatus
from graphrag.services.ingestion.parser import DocumentParser
from graphrag.services.ingestion.service import (
    DeletionService,
    IngestionService,
    ProjectionService,
)
from tests.fakes import FakeClock, FakeEmbedder, FakeGraphStore, FakeJobQueue
from tests.unit._settings_helpers import set_required_secrets

pytestmark = pytest.mark.integration

_QDRANT_URL = "http://localhost:6333"
_DIM = 4

# Small enough that this whole string is ONE chunk under the default chunk_size (900), so two
# documents built from it always produce the exact same content-addressed chunk_id.
_SHARED_PARAGRAPH = (
    "Apple revolutionized personal technology with the introduction of the Macintosh. "
    "Today Apple leads the industry in innovation across every product line it ships."
)


@pytest.fixture
def ingestion_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    set_required_secrets(monkeypatch)
    suffix = uuid.uuid4().hex[:8]
    base = Settings()
    vector = base.retrieval.vector.model_copy(update={"collection": f"test_ing_chunks_{suffix}"})
    retrieval = base.retrieval.model_copy(update={"vector": vector})
    resolution = base.resolution.model_copy(update={"collection": f"test_ing_entities_{suffix}"})
    dense = base.embedding.dense.model_copy(update={"dimensions": _DIM})
    embedding = base.embedding.model_copy(update={"dense": dense})
    return base.model_copy(
        update={"retrieval": retrieval, "resolution": resolution, "embedding": embedding}
    )


@pytest.fixture
async def qdrant_client() -> AsyncIterator[AsyncQdrantClient]:
    client = AsyncQdrantClient(url=_QDRANT_URL, prefer_grpc=False, timeout=10)
    yield client
    await client.close()


@pytest.fixture
async def vector_store(
    qdrant_client: AsyncQdrantClient, ingestion_settings: Settings
) -> AsyncIterator[QdrantVectorStore]:
    store = QdrantVectorStore(qdrant_client, ingestion_settings)
    await store.ensure_collections()
    yield store
    for name in (
        ingestion_settings.retrieval.vector.collection,
        ingestion_settings.resolution.collection,
    ):
        with contextlib.suppress(Exception):
            await qdrant_client.delete_collection(name)


@pytest.fixture
def ledger(clean_pg: AsyncEngine) -> PostgresDocumentLedger:
    return PostgresDocumentLedger(clean_pg)


@pytest.fixture
def sources(clean_pg: AsyncEngine) -> PostgresSourceRegistry:
    return PostgresSourceRegistry(clean_pg)


@pytest.fixture
def graph_store() -> FakeGraphStore:
    return FakeGraphStore()


@pytest.fixture
def job_queue() -> FakeJobQueue:
    return FakeJobQueue()


@pytest.fixture
def ingestion_service(
    vector_store: QdrantVectorStore,
    graph_store: FakeGraphStore,
    sources: PostgresSourceRegistry,
    ledger: PostgresDocumentLedger,
    job_queue: FakeJobQueue,
    ingestion_settings: Settings,
) -> IngestionService:
    return IngestionService(
        parser=DocumentParser(),
        embedder=FakeEmbedder(dimensions=_DIM),
        vector_store=vector_store,
        graph_store=graph_store,
        source_registry=sources,
        ledger=ledger,
        job_queue=job_queue,
        clock=FakeClock(),
        ingestion=ingestion_settings.ingestion,
    )


async def _register_and_ingest(
    service: IngestionService, ledger: PostgresDocumentLedger, doc_id: str, uri: str, text: str
) -> None:
    sha256 = hashlib.sha256(doc_id.encode()).hexdigest()
    await ledger.register(doc_id, uri, sha256, "text/plain")
    await service.ingest(
        doc_id, text.encode("utf-8"), "text/plain", uri, correlation_id=f"cid-{doc_id}"
    )


async def test_shared_paragraph_two_sources(
    ingestion_service: IngestionService,
    ledger: PostgresDocumentLedger,
    sources: PostgresSourceRegistry,
    vector_store: QdrantVectorStore,
) -> None:
    """Two documents sharing a paragraph -> ONE Qdrant point, len(sources) == 2."""
    await _register_and_ingest(
        ingestion_service, ledger, "doc-a", "file:///a.txt", _SHARED_PARAGRAPH
    )
    await _register_and_ingest(
        ingestion_service, ledger, "doc-b", "file:///b.txt", _SHARED_PARAGRAPH
    )

    expected_id = chunk_id(_SHARED_PARAGRAPH)
    fetched = await vector_store.get_chunks([expected_id])
    assert len(fetched) == 1

    all_sources = await sources.sources_for([expected_id])
    assert {ref.doc_id for ref in all_sources[expected_id]} == {"doc-a", "doc-b"}


async def test_reingest_same_doc_idempotent(
    ingestion_service: IngestionService,
    ledger: PostgresDocumentLedger,
    sources: PostgresSourceRegistry,
    vector_store: QdrantVectorStore,
) -> None:
    await _register_and_ingest(
        ingestion_service, ledger, "doc-a", "file:///a.txt", _SHARED_PARAGRAPH
    )
    expected_id = chunk_id(_SHARED_PARAGRAPH)
    before = await sources.sources_for([expected_id])
    assert len(before[expected_id]) == 1

    # re-run ingest() for the SAME doc_id (e.g. a retried worker job)
    await ingestion_service.ingest(
        "doc-a",
        _SHARED_PARAGRAPH.encode("utf-8"),
        "text/plain",
        "file:///a.txt",
        correlation_id="cid-retry",
    )

    after = await sources.sources_for([expected_id])
    assert len(after[expected_id]) == 1  # no duplicate row

    fetched = await vector_store.get_chunks([expected_id])
    assert len(fetched) == 1  # no duplicate point


async def test_concurrent_ingest_no_lost_update(
    ingestion_service: IngestionService,
    ledger: PostgresDocumentLedger,
    sources: PostgresSourceRegistry,
) -> None:
    """8 docs sharing a paragraph, ingested CONCURRENTLY -> 8 rows in Postgres, and after
    projection, all 8 show up in the single shared Qdrant point's sources[]."""
    doc_ids = [f"doc-{i}" for i in range(8)]
    for doc_id in doc_ids:
        sha256 = hashlib.sha256(doc_id.encode()).hexdigest()
        await ledger.register(doc_id, f"file:///{doc_id}.txt", sha256, "text/plain")

    async with asyncio.TaskGroup() as tg:
        for doc_id in doc_ids:
            tg.create_task(
                ingestion_service.ingest(
                    doc_id,
                    _SHARED_PARAGRAPH.encode("utf-8"),
                    "text/plain",
                    f"file:///{doc_id}.txt",
                    correlation_id=f"cid-{doc_id}",
                )
            )

    expected_id = chunk_id(_SHARED_PARAGRAPH)
    all_sources = await sources.sources_for([expected_id])
    assert len(all_sources[expected_id]) == 8
    assert {ref.doc_id for ref in all_sources[expected_id]} == set(doc_ids)


async def test_projection_repairs_lost_update_after_concurrent_ingest(
    ingestion_service: IngestionService,
    ledger: PostgresDocumentLedger,
    sources: PostgresSourceRegistry,
    vector_store: QdrantVectorStore,
) -> None:
    doc_ids = [f"doc-{i}" for i in range(8)]
    for doc_id in doc_ids:
        sha256 = hashlib.sha256(doc_id.encode()).hexdigest()
        await ledger.register(doc_id, f"file:///{doc_id}.txt", sha256, "text/plain")

    async with asyncio.TaskGroup() as tg:
        for doc_id in doc_ids:
            tg.create_task(
                ingestion_service.ingest(
                    doc_id,
                    _SHARED_PARAGRAPH.encode("utf-8"),
                    "text/plain",
                    f"file:///{doc_id}.txt",
                    correlation_id=f"cid-{doc_id}",
                )
            )

    expected_id = chunk_id(_SHARED_PARAGRAPH)
    projection = ProjectionService(source_registry=sources, vector_store=vector_store)
    await projection.project([expected_id])

    [fetched] = await vector_store.get_chunks([expected_id])
    assert {ref.doc_id for ref in fetched.sources} == set(doc_ids)


async def test_delete_decrements_sources(
    ingestion_service: IngestionService,
    ledger: PostgresDocumentLedger,
    sources: PostgresSourceRegistry,
    vector_store: QdrantVectorStore,
    graph_store: FakeGraphStore,
) -> None:
    """A 2-source chunk survives with 1 remaining source after one document is deleted."""
    await _register_and_ingest(
        ingestion_service, ledger, "doc-a", "file:///a.txt", _SHARED_PARAGRAPH
    )
    await _register_and_ingest(
        ingestion_service, ledger, "doc-b", "file:///b.txt", _SHARED_PARAGRAPH
    )
    expected_id = chunk_id(_SHARED_PARAGRAPH)

    projection = ProjectionService(source_registry=sources, vector_store=vector_store)
    deletion = DeletionService(
        source_registry=sources, projection=projection, graph_store=graph_store, ledger=ledger
    )
    await deletion.delete("doc-a")

    remaining = await sources.sources_for([expected_id])
    assert [ref.doc_id for ref in remaining[expected_id]] == ["doc-b"]

    fetched = await vector_store.get_chunks([expected_id])
    assert len(fetched) == 1  # chunk survives — still referenced by doc-b


async def test_delete_last_source_removes_from_both_stores(
    ingestion_service: IngestionService,
    ledger: PostgresDocumentLedger,
    sources: PostgresSourceRegistry,
    vector_store: QdrantVectorStore,
    graph_store: FakeGraphStore,
) -> None:
    unique_text = "Only doc-solo contains this exact sentence about a rare topic."
    await _register_and_ingest(
        ingestion_service, ledger, "doc-solo", "file:///solo.txt", unique_text
    )
    expected_id = chunk_id(unique_text)

    projection = ProjectionService(source_registry=sources, vector_store=vector_store)
    deletion = DeletionService(
        source_registry=sources, projection=projection, graph_store=graph_store, ledger=ledger
    )
    await deletion.delete("doc-solo")

    assert await vector_store.get_chunks([expected_id]) == []
    assert "doc-solo" not in graph_store.documents
    record = await ledger.get("doc-solo")
    assert record is not None
    assert record.status == DocumentStatus.DELETING
