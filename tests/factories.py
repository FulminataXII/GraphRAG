"""Build valid domain objects with sensible defaults. See BLUEPRINT §9.

`make_state` (a `QueryState` factory) isn't here yet: `QueryState` is declared in
`services/orchestration/` and built in BO-10 (BUILD_ORDER.md). Adding it now would mean
importing a type that doesn't exist — a dependency-order bug, not a BO-01 concern. It lands
here alongside that BO.
"""

from __future__ import annotations

from datetime import UTC, datetime

from graphrag.core.ids import chunk_id, content_hash
from graphrag.core.models import Chunk, SourceRef


def make_source_ref(
    *,
    doc_id: str = "doc-1",
    uri: str = "file:///doc-1.txt",
    page: int | None = None,
    char_start: int = 0,
    char_end: int = 10,
    ingested_at: datetime | None = None,
) -> SourceRef:
    return SourceRef(
        doc_id=doc_id,
        uri=uri,
        page=page,
        char_start=char_start,
        char_end=char_end,
        ingested_at=ingested_at or datetime(2024, 1, 1, tzinfo=UTC),
    )


def make_chunk(text: str = "default chunk text", sources: list[SourceRef] | None = None) -> Chunk:
    return Chunk(
        chunk_id=chunk_id(text),
        text=text,
        content_hash=content_hash(text),
        sources=sources if sources is not None else [make_source_ref()],
        entity_ids=[],
    )
