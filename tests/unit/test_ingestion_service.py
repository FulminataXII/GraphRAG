"""`IngestionService`/`ProjectionService`/`DeletionService` unit tests. See BLUEPRINT §6.1."""

from __future__ import annotations

import ast
from pathlib import Path
from uuid import UUID

from graphrag.core.ids import chunk_id
from graphrag.services.ingestion.service import DeletionService, ProjectionService
from tests.factories import make_source_ref
from tests.fakes import FakeSourceRegistry, FakeVectorStore

_SERVICE_PATH = (
    Path(__file__).resolve().parents[2] / "graphrag" / "services" / "ingestion" / "service.py"
)


def test_ingestion_never_calls_set_sources_directly() -> None:
    """Static check: `IngestionService` never references `set_sources` anywhere in its own
    module. Only `ProjectionService` may write Qdrant's sources[] (BLUEPRINT §6.1)."""
    tree = ast.parse(_SERVICE_PATH.read_text(encoding="utf-8"))
    ingestion_service_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "IngestionService"
    )
    offenders = [
        node.attr
        for node in ast.walk(ingestion_service_node)
        if isinstance(node, ast.Attribute) and node.attr == "set_sources"
    ]
    assert not offenders, "IngestionService must never call VectorStore.set_sources directly"


async def test_projection_is_convergent() -> None:
    """Running project() twice yields the identical payload; running it over a hand-corrupted
    payload repairs it."""
    sources = FakeSourceRegistry()
    vector = FakeVectorStore()

    chunk_a = chunk_id("shared paragraph text")
    ref1 = make_source_ref(doc_id="doc-1", char_start=0, char_end=20)
    ref2 = make_source_ref(doc_id="doc-2", char_start=5, char_end=25)
    await sources.add([(chunk_a, ref1), (chunk_a, ref2)])

    vector.chunks[chunk_a] = _fake_chunk(chunk_a, sources=[ref1])
    vector.dense[chunk_a] = [0.0]
    vector.sparse[chunk_a] = _empty_sparse()

    projection = ProjectionService(source_registry=sources, vector_store=vector)

    await projection.project([chunk_a])
    first_sources = {s.doc_id for s in vector.chunks[chunk_a].sources}
    assert first_sources == {"doc-1", "doc-2"}

    # run again — identical result
    await projection.project([chunk_a])
    assert {s.doc_id for s in vector.chunks[chunk_a].sources} == {"doc-1", "doc-2"}

    # hand-corrupt the payload, then re-run — repaired
    vector.chunks[chunk_a] = vector.chunks[chunk_a].model_copy(update={"sources": []})
    await projection.project([chunk_a])
    assert {s.doc_id for s in vector.chunks[chunk_a].sources} == {"doc-1", "doc-2"}


async def test_projection_deletes_orphaned_chunk_from_vector_store() -> None:
    sources = FakeSourceRegistry()
    vector = FakeVectorStore()
    orphan = chunk_id("no longer referenced text")
    vector.chunks[orphan] = _fake_chunk(orphan, sources=[])
    vector.dense[orphan] = [0.0]
    vector.sparse[orphan] = _empty_sparse()

    projection = ProjectionService(source_registry=sources, vector_store=vector)
    await projection.project([orphan])

    assert orphan not in vector.chunks


async def test_projection_empty_input_is_a_noop() -> None:
    sources = FakeSourceRegistry()
    vector = FakeVectorStore()
    projection = ProjectionService(source_registry=sources, vector_store=vector)
    await projection.project([])  # must not raise


async def test_deletion_removes_document_sources_and_marks_deleting() -> None:
    from graphrag.core.models import DocumentStatus
    from tests.fakes import FakeDocumentLedger, FakeGraphStore

    sources = FakeSourceRegistry()
    vector = FakeVectorStore()
    graph = FakeGraphStore()
    ledger = FakeDocumentLedger()

    chunk = chunk_id("doc only content")
    ref = make_source_ref(doc_id="doc-x", char_start=0, char_end=10)
    await sources.add([(chunk, ref)])
    vector.chunks[chunk] = _fake_chunk(chunk, sources=[ref])
    vector.dense[chunk] = [0.0]
    vector.sparse[chunk] = _empty_sparse()

    await ledger.register("doc-x", "file:///doc-x.txt", "sha", "text/plain")

    projection = ProjectionService(source_registry=sources, vector_store=vector)
    deletion = DeletionService(
        source_registry=sources, projection=projection, graph_store=graph, ledger=ledger
    )

    await deletion.delete("doc-x")

    assert chunk not in vector.chunks
    record = await ledger.get("doc-x")
    assert record is not None
    assert record.status == DocumentStatus.DELETING


def _fake_chunk(cid: UUID, *, sources: list) -> object:
    from graphrag.core.ids import content_hash
    from graphrag.core.models import Chunk

    return Chunk(
        chunk_id=cid,
        text="placeholder",
        content_hash=content_hash("placeholder"),
        sources=sources,
        entity_ids=[],
    )


def _empty_sparse() -> object:
    from graphrag.core.models import SparseVector

    return SparseVector(indices=[], values=[])
