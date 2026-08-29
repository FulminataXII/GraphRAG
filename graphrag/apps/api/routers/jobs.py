"""`GET /v1/jobs/{id}`. See BLUEPRINT §7.1.

`{id}` is a job_id — but `documents.py` always enqueues `ingest_document`/`delete_document`
jobs with `job_id=doc_id` (content-addressed, so it doubles as arq's dedup key), so this one
path parameter can look up BOTH the job state and the document status.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from graphrag.apps.api.deps import get_container
from graphrag.apps.api.main import Container
from graphrag.core.errors import NotFound

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


@router.get("/{job_id}")
async def get_job(job_id: str, container: Container = Depends(get_container)) -> dict[str, object]:
    """Job state + document status."""
    job_status = await container.job_queue.status(job_id)
    document = await container.ledger.get(job_id)

    if job_status.state == "not_found" and document is None:
        raise NotFound(f"no job or document {job_id!r}", details={"job_id": job_id})

    body: dict[str, object] = {
        "job_id": job_status.job_id,
        "state": job_status.state,
        "attempts": job_status.attempts,
        "error": job_status.error,
    }
    if document is not None:
        body["doc_id"] = document.doc_id
        body["document_status"] = document.status.value
    return body
