"""`delete_document` arq task. See BLUEPRINT §7.2 / §6.1's `DeletionService`."""

from __future__ import annotations

import logging
from typing import Any

from graphrag.apps.worker.tasks._common import run_task
from graphrag.core.errors import AppError, ConflictError
from graphrag.core.events import DeleteDocumentPayload, JobEnvelope
from graphrag.core.models import DocumentStatus
from graphrag.services.ingestion.service import DeletionService, ProjectionService

_log = logging.getLogger(__name__)


async def delete_document(ctx: dict[str, Any], env: JobEnvelope[DeleteDocumentPayload]) -> None:
    """See `ingest_document`'s docstring for the contract shared by every task in this package."""
    container = ctx["container"]

    async def _on_failure(exc: BaseException) -> None:
        # DeletionService.delete() may have already moved the ledger row to DELETING — a
        # terminal state with no forward transitions (BLUEPRINT §5.4's ledger state machine) —
        # before failing on a later step. Attempting FAILED at that point would itself raise
        # ConflictError and mask the real error, so this is best-effort, not a hard requirement.
        code = exc.code if isinstance(exc, AppError) else "INTERNAL_ERROR"
        try:
            await container.ledger.set_status(
                env.payload.doc_id, DocumentStatus.FAILED, error_code=code
            )
        except ConflictError:
            _log.warning(
                "could not mark document FAILED after delete_document error "
                "(ledger already past a terminal status)",
                extra={"doc_id": env.payload.doc_id},
            )

    async def _body() -> None:
        projection = ProjectionService(
            source_registry=container.sources, vector_store=container.vector_store
        )
        service = DeletionService(
            source_registry=container.sources,
            projection=projection,
            graph_store=container.graph_store,
            ledger=container.ledger,
        )
        await service.delete(env.payload.doc_id)

    await run_task("delete_document", ctx, env, _body, on_failure=_on_failure)
