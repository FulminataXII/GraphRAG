"""Versioned queue payload schemas. See BLUEPRINT §3.4."""

from __future__ import annotations

from typing import Final
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from graphrag.core.models import Mention

SCHEMA_VERSION: Final[int] = 1


class JobEnvelope[P](BaseModel):
    """Every queue message. Passed as a positional job ARGUMENT, never via arq's ctx.

    Contract:
        - `otel` carries the W3C traceparent injected at enqueue time.
        - Workers reject an envelope whose schema_version major differs from SCHEMA_VERSION.
        - Must be JSON-serializable end to end.
    """

    schema_version: int = SCHEMA_VERSION
    correlation_id: str
    otel: dict[str, str]
    enqueued_at: AwareDatetime
    payload: P


class IngestDocumentPayload(BaseModel):
    doc_id: str
    uri: str
    sha256: str
    mime_type: str


class ExtractEntitiesPayload(BaseModel):
    doc_id: str
    chunk_ids: list[UUID]


class UnresolvedRelation(BaseModel):
    """A relation the LLM extracted whose endpoints are still SURFACE FORMS, not canonical
    entity ids — `Relation` (core/models.py) requires `src_id`/`dst_id: UUID`, which don't exist
    until `ResolutionService.resolve()` has run, so a just-extracted relation cannot be a
    `Relation` yet. This is `resolve_entities`' own job to finish: build a surface -> canonical_id
    map from `ResolutionResult.entities` (name + aliases) and look each endpoint up in it.

    SPEC GAP (BO-08): the natural shape for this is already declared — `RelationOut`
    (services/orchestration/schemas.py, chunk_id/src_surface/dst_surface/type/confidence/
    evidence_span) — but it lives in `services/`, and `core/` may not import `services/`
    (BLUEPRINT §0's layering table; `scripts/check_layering.py` enforces it). BLUEPRINT §1a's
    Type Index has no slot for an unresolved-relation carrier in `core/events.py` either. Neither
    document anticipated that giving relations a sink (this BO's own build step 2, "Wire graph
    writes into IngestionService / resolve task") requires something shaped like `RelationOut` to
    survive a queue hop through a `core/` payload. This type is that: a layering-legal,
    structurally identical twin of `RelationOut`, defined here rather than imported. Reported
    rather than silently added.
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: UUID
    src_surface: str
    dst_surface: str
    type: str
    confidence: float
    evidence_span: str


class ResolveEntitiesPayload(BaseModel):
    """Carries `extract_entities`' output to `resolve_entities`.

    SPEC GAP (BO-07): BLUEPRINT §1a's Type Index lists exactly four payloads for this module,
    with none for `resolve_entities` — yet BLUEPRINT §7.2 requires `resolve_entities` to be its
    OWN registered arq function, separate from `extract_entities` (ARCHITECTURE §1.3's sequence
    diagram instead folds extraction and resolution into one worker step with no queue hop
    between them; BLUEPRINT wins on this disagreement per its own precedence rule, and the
    disagreement is reported here rather than silently picking a side). Something has to carry
    `extract_entities`' output across that queue hop; this reuses the already-Type-Indexed
    `Mention` (core/models.py) rather than inventing a new domain concept.

    `relations` (BO-08 addition): `extract_entities` no longer discards the LLM's relation
    output — see `UnresolvedRelation`. Defaults to `[]` so BO-07-era callers that never populated
    it still construct validly.
    """

    doc_id: str
    mentions: list[Mention]
    relations: list[UnresolvedRelation] = Field(default_factory=list)


class ProjectPayloadPayload(BaseModel):
    """Batch of chunk_ids whose Qdrant payload must be re-derived from Postgres."""

    chunk_ids: list[UUID]


class DeleteDocumentPayload(BaseModel):
    doc_id: str
