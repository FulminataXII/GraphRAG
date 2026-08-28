"""`PostgresSourceRegistry` against a real Postgres. See BLUEPRINT §5.4 / BUILD_ORDER BO-03.

Marked `integration` for the same reason as `test_postgres_ledger.py`: `add()`'s ON CONFLICT
dedup under real concurrent transactions is exactly the property being tested, and no in-memory
substitute in this project's dependency set can stand in for it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from graphrag.adapters.postgres.sources import PostgresSourceRegistry
from graphrag.core.models import SourceRef

pytestmark = pytest.mark.integration


def _ref(doc_id: str) -> SourceRef:
    return SourceRef(
        doc_id=doc_id,
        uri=f"file:///{doc_id}.txt",
        page=None,
        char_start=0,
        char_end=10,
        ingested_at=datetime.now(UTC),
    )


async def test_source_registry_add_idempotent(clean_pg: AsyncEngine) -> None:
    registry = PostgresSourceRegistry(clean_pg)
    chunk_id = uuid4()
    await registry.add([(chunk_id, _ref("doc-x"))])
    await registry.add([(chunk_id, _ref("doc-x"))])

    sources = await registry.sources_for([chunk_id])
    assert len(sources[chunk_id]) == 1


async def test_source_registry_concurrent_add(clean_pg: AsyncEngine) -> None:
    registry = PostgresSourceRegistry(clean_pg)
    chunk_id = uuid4()
    doc_ids = [f"doc-{i}" for i in range(16)]

    await asyncio.gather(*[registry.add([(chunk_id, _ref(doc_id))]) for doc_id in doc_ids])

    sources = await registry.sources_for([chunk_id])
    assert len(sources[chunk_id]) == 16
    assert {ref.doc_id for ref in sources[chunk_id]} == set(doc_ids)


async def test_orphaned_chunks_single_query(clean_pg: AsyncEngine) -> None:
    registry = PostgresSourceRegistry(clean_pg)
    has_source = uuid4()
    orphan = uuid4()
    await registry.add([(has_source, _ref("doc-keep"))])

    orphaned = await registry.orphaned_chunks([has_source, orphan])

    assert orphaned == [orphan]


async def test_remove_document_returns_affected_chunks(clean_pg: AsyncEngine) -> None:
    registry = PostgresSourceRegistry(clean_pg)
    chunk_a, chunk_b = uuid4(), uuid4()
    await registry.add([(chunk_a, _ref("doc-remove")), (chunk_b, _ref("doc-remove"))])

    affected = await registry.remove_document("doc-remove")

    assert set(affected) == {chunk_a, chunk_b}
    remaining = await registry.sources_for([chunk_a, chunk_b])
    assert remaining[chunk_a] == []
    assert remaining[chunk_b] == []
