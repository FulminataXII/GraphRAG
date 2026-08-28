"""Versioned queue payload schemas. See BLUEPRINT §3.4."""

from __future__ import annotations

from typing import Final
from uuid import UUID

from pydantic import AwareDatetime, BaseModel

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


class ProjectPayloadPayload(BaseModel):
    """Batch of chunk_ids whose Qdrant payload must be re-derived from Postgres."""

    chunk_ids: list[UUID]


class DeleteDocumentPayload(BaseModel):
    doc_id: str
