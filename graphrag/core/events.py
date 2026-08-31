"""Versioned queue payload schemas. See BLUEPRINT §3.4."""

from __future__ import annotations

from typing import Final
from uuid import UUID

from pydantic import AwareDatetime, BaseModel

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


class ResolveEntitiesPayload(BaseModel):
    """Carries `extract_entities`' output to `resolve_entities`.

    SPEC GAP (BO-07): BLUEPRINT §1a's Type Index lists exactly four payloads for this module,
    with none for `resolve_entities` — yet BLUEPRINT §7.2 requires `resolve_entities` to be its
    OWN registered arq function, separate from `extract_entities` (ARCHITECTURE §1.3's sequence
    diagram instead folds extraction and resolution into one worker step with no queue hop
    between them; BLUEPRINT wins on this disagreement per its own precedence rule, and the
    disagreement is reported here rather than silently picking a side). Something has to carry
    `extract_entities`' output across that queue hop; this reuses the already-Type-Indexed
    `Mention` (core/models.py) rather than inventing a new domain concept. Relations are not
    carried here: `GraphStore.upsert_relations` (the only sink for them) doesn't exist until
    BO-08, so `resolve_entities` in this BO has nothing to do with them yet.
    """

    doc_id: str
    mentions: list[Mention]


class ProjectPayloadPayload(BaseModel):
    """Batch of chunk_ids whose Qdrant payload must be re-derived from Postgres."""

    chunk_ids: list[UUID]


class DeleteDocumentPayload(BaseModel):
    doc_id: str
