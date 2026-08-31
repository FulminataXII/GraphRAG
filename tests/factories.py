"""Build valid domain objects with sensible defaults. See BLUEPRINT §9.

`make_state` (a `QueryState` factory) isn't here yet: `QueryState` is declared in
`services/orchestration/` and built in BO-10 (BUILD_ORDER.md). Adding it now would mean
importing a type that doesn't exist — a dependency-order bug, not a BO-01 concern. It lands
here alongside that BO.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from graphrag.adapters.telemetry.metrics import Metrics
from graphrag.core.ids import chunk_id, content_hash, entity_id
from graphrag.core.models import Chunk, Entity, EntityType, Mention, Relation, SourceRef


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


def make_metrics() -> tuple[Metrics, InMemoryMetricReader]:
    """A real `Metrics` backed by an in-memory reader — no mocks, and any test that cares can
    inspect emitted values via the returned reader (see `graphrag.adapters.telemetry.metrics`)."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    return Metrics(provider.get_meter("test")), reader


def make_chunk(text: str = "default chunk text", sources: list[SourceRef] | None = None) -> Chunk:
    return Chunk(
        chunk_id=chunk_id(text),
        text=text,
        content_hash=content_hash(text),
        sources=sources if sources is not None else [make_source_ref()],
        entity_ids=[],
    )


def make_entity(
    *,
    name: str = "Acme",
    type: EntityType = EntityType.ORG,
    aliases: list[str] | None = None,
    mention_count: int = 1,
    canonical_id: UUID | None = None,
) -> Entity:
    return Entity(
        canonical_id=canonical_id or entity_id(name, type.value),
        name=name,
        name_normalized=name.lower(),
        type=type,
        aliases=aliases if aliases is not None else [],
        mention_count=mention_count,
    )


def make_mention(
    *,
    surface: str = "Acme",
    type: EntityType = EntityType.ORG,
    chunk_id: UUID | None = None,
    char_start: int = 0,
    char_end: int = 4,
    confidence: float = 0.9,
    entity_id: UUID | None = None,
) -> Mention:
    return Mention(
        surface=surface,
        type=type,
        chunk_id=chunk_id if chunk_id is not None else uuid4(),
        char_start=char_start,
        char_end=char_end,
        confidence=confidence,
        entity_id=entity_id,
    )


def make_relation(
    *,
    src_id: UUID | None = None,
    dst_id: UUID | None = None,
    type: str = "RELATED_TO",
    confidence: float = 0.9,
    chunk_id: UUID | None = None,
    doc_id: str = "doc-1",
    evidence_span: str = "evidence sentence",
) -> Relation:
    return Relation(
        src_id=src_id if src_id is not None else uuid4(),
        dst_id=dst_id if dst_id is not None else uuid4(),
        type=type,
        confidence=confidence,
        chunk_id=chunk_id if chunk_id is not None else uuid4(),
        doc_id=doc_id,
        evidence_span=evidence_span,
    )
