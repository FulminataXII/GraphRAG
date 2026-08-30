"""`ingest_document` arq task. See BLUEPRINT §7.2."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from graphrag.adapters.clock import SystemClock
from graphrag.apps.worker.tasks._common import run_task
from graphrag.core.errors import AppError
from graphrag.core.events import IngestDocumentPayload, JobEnvelope
from graphrag.core.models import DocumentStatus
from graphrag.services.ingestion.parser import DocumentParser
from graphrag.services.ingestion.service import IngestionService


def _read_uri(uri: str) -> bytes:
    """Resolve a `file://` URI to bytes — see `apps/worker/tasks/__init__.py`'s docstring for
    the local-disk upload convention this depends on."""
    if not uri.startswith("file://"):
        raise ValueError(f"unsupported uri scheme: {uri!r}")
    return Path(uri.removeprefix("file://")).read_bytes()


async def ingest_document(ctx: dict[str, Any], env: JobEnvelope[IngestDocumentPayload]) -> None:
    """`ctx` is arq's dict (redis, job_id, job_try, enqueue_time) — it does NOT carry the
    payload. The envelope arrives as a positional argument.

    Contract (identical for all tasks):
        - Reject envelopes whose schema_version major differs from SCHEMA_VERSION.
        - restore_context(env) -> start a span as its child, so the trace spans the queue hop.
        - bind_request_context(correlation_id=..., job_id=ctx['job_id'], job_try=ctx['job_try'])
        - On final failure: set ledger status FAILED with the error code, then re-raise so arq
          records it.
    """
    container = ctx["container"]

    async def _on_failure(exc: BaseException) -> None:
        code = exc.code if isinstance(exc, AppError) else "INTERNAL_ERROR"
        await container.ledger.set_status(
            env.payload.doc_id, DocumentStatus.FAILED, error_code=code
        )

    async def _body() -> None:
        service = IngestionService(
            parser=DocumentParser(),
            embedder=container.embedder,
            vector_store=container.vector_store,
            graph_store=container.graph_store,
            source_registry=container.sources,
            ledger=container.ledger,
            job_queue=container.job_queue,
            clock=SystemClock(),
            metrics=container.metrics,
            ingestion=container.settings.ingestion,
        )
        raw = _read_uri(env.payload.uri)
        await service.ingest(
            env.payload.doc_id,
            raw,
            env.payload.mime_type,
            env.payload.uri,
            correlation_id=env.correlation_id,
        )

    await run_task("ingest_document", ctx, env, _body, on_failure=_on_failure)
