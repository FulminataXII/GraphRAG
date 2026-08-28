"""PostgresSourceRegistry — implements `core.ports.SourceRegistry`. See BLUEPRINT §5.4.

`chunk_sources` PK(chunk_id, doc_id) is the concurrency arbiter for ARCHITECTURE §1.3's
read-modify-write across documents that share a paragraph: every writer does an
`INSERT ... ON CONFLICT DO NOTHING`, so N concurrent workers racing to record the same
(chunk_id, doc_id) pair converge on exactly one row with no lock and no shard.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from graphrag.adapters.postgres.tables import chunk_sources
from graphrag.core.models import SourceRef

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


def _row_to_source_ref(row: object) -> SourceRef:
    return SourceRef(
        doc_id=row.doc_id,  # type: ignore[attr-defined]
        uri=row.uri,  # type: ignore[attr-defined]
        page=row.page,  # type: ignore[attr-defined]
        char_start=row.char_start,  # type: ignore[attr-defined]
        char_end=row.char_end,  # type: ignore[attr-defined]
        ingested_at=row.ingested_at,  # type: ignore[attr-defined]
    )


class PostgresSourceRegistry:
    """Implements `SourceRegistry`.

    Contract (BLUEPRINT §5.4):
        - add() is one executemany INSERT ... ON CONFLICT (chunk_id, doc_id) DO NOTHING.
          Safe under unlimited worker concurrency; no lock, no shard.
        - remove_document() returns affected chunk_ids so the caller can re-project.
        - orphaned_chunks() is a single query, not a per-chunk loop.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(self, refs: Sequence[tuple[UUID, SourceRef]]) -> None:
        if not refs:
            return
        rows = [
            {
                "chunk_id": chunk_id,
                "doc_id": ref.doc_id,
                "uri": ref.uri,
                "page": ref.page,
                "char_start": ref.char_start,
                "char_end": ref.char_end,
                "ingested_at": ref.ingested_at,
            }
            for chunk_id, ref in refs
        ]
        stmt = pg_insert(chunk_sources).on_conflict_do_nothing(
            index_elements=["chunk_id", "doc_id"]
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt, rows)

    async def sources_for(self, chunk_ids: Sequence[UUID]) -> dict[UUID, list[SourceRef]]:
        result: dict[UUID, list[SourceRef]] = {chunk_id: [] for chunk_id in chunk_ids}
        if not chunk_ids:
            return result
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                select(chunk_sources).where(chunk_sources.c.chunk_id.in_(chunk_ids))
            )
            for row in rows:
                result.setdefault(row.chunk_id, []).append(_row_to_source_ref(row))
        return result

    async def remove_document(self, doc_id: str) -> list[UUID]:
        async with self._engine.begin() as conn:
            rows = await conn.execute(
                chunk_sources.delete()
                .where(chunk_sources.c.doc_id == doc_id)
                .returning(chunk_sources.c.chunk_id)
            )
            return [row.chunk_id for row in rows]

    async def orphaned_chunks(self, chunk_ids: Sequence[UUID]) -> list[UUID]:
        if not chunk_ids:
            return []
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                select(chunk_sources.c.chunk_id.distinct()).where(
                    chunk_sources.c.chunk_id.in_(chunk_ids)
                )
            )
            remaining = {row.chunk_id for row in rows}
        return [chunk_id for chunk_id in chunk_ids if chunk_id not in remaining]
