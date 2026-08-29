"""`POST /v1/documents`, `DELETE /v1/documents/{id}`. See BLUEPRINT §7.1.

Uploaded bytes are written to a local directory shared with the worker via a docker-compose
bind mount — see `apps/worker/tasks/__init__.py`'s docstring for the spec gap (no object-storage
adapter exists anywhere in this codebase) this convention works around.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import magic
from fastapi import APIRouter, Depends, File, Header, UploadFile
from fastapi.responses import JSONResponse

from graphrag.apps._upload_storage import persist_upload
from graphrag.apps.api.deps import get_container
from graphrag.apps.api.main import Container
from graphrag.core.errors import ValidationError
from graphrag.core.events import DeleteDocumentPayload, IngestDocumentPayload, JobEnvelope
from graphrag.core.ids import new_correlation_id
from graphrag.services.ingestion.service import document_id, document_sha256

router = APIRouter(prefix="/v1/documents", tags=["documents"])


@router.post("", status_code=202)
async def create_document(
    file: Annotated[UploadFile, File()],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    container: Container = Depends(get_container),
) -> JSONResponse:
    """Multipart upload. Requires `Idempotency-Key`. Validates MIME by CONTENT SNIFFING (never
    the client-declared `Content-Type`), plus a size cap. Returns 202 `{job_id, doc_id,
    correlation_id}` for a new document, or 200 with the SAME shape (no new job) when the
    content (by sha256) is already registered — content-addressing is what makes a re-upload
    under a different Idempotency-Key still resolve to the original doc_id.
    """
    if not idempotency_key:
        raise ValidationError("the Idempotency-Key header is required")

    raw = await file.read()
    limits = container.settings.limits
    max_bytes = limits.max_upload_mb * 1024 * 1024
    if len(raw) > max_bytes:
        raise ValidationError(
            f"upload exceeds the {limits.max_upload_mb} MB limit",
            details={"max_upload_mb": limits.max_upload_mb},
        )

    sniffed_mime = magic.from_buffer(raw, mime=True)
    if sniffed_mime not in limits.allowed_upload_mimetypes:
        raise ValidationError(
            f"unsupported content type: {sniffed_mime!r}",
            details={"sniffed_mime_type": sniffed_mime},
        )

    doc_id = document_id(raw)
    sha256 = document_sha256(raw)
    correlation_id = new_correlation_id()
    uri = persist_upload(raw, doc_id)

    is_new = await container.ledger.register(doc_id, uri, sha256, sniffed_mime)
    if not is_new:
        return JSONResponse(
            status_code=200,
            content={"job_id": doc_id, "doc_id": doc_id, "correlation_id": correlation_id},
        )

    job_id = await container.job_queue.enqueue(
        "ingest_document",
        JobEnvelope(
            correlation_id=correlation_id,
            otel={},
            enqueued_at=datetime.now(UTC),
            payload=IngestDocumentPayload(
                doc_id=doc_id, uri=uri, sha256=sha256, mime_type=sniffed_mime
            ),
        ),
        job_id=doc_id,
    )
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "doc_id": doc_id, "correlation_id": correlation_id},
    )


@router.delete("/{doc_id}", status_code=202)
async def delete_document(
    doc_id: str, container: Container = Depends(get_container)
) -> JSONResponse:
    """Enqueues `DeleteDocument`. Returns 202. Never deletes inline — see
    `services.ingestion.service.DeletionService`."""
    correlation_id = new_correlation_id()
    job_id = await container.job_queue.enqueue(
        "delete_document",
        JobEnvelope(
            correlation_id=correlation_id,
            otel={},
            enqueued_at=datetime.now(UTC),
            payload=DeleteDocumentPayload(doc_id=doc_id),
        ),
        job_id=doc_id,
    )
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "doc_id": doc_id, "correlation_id": correlation_id},
    )
