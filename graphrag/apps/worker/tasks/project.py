"""`project_chunk_payload` arq task. See BLUEPRINT §7.2 / §6.1's `ProjectionService`.

Routed to the projection queue (`ingestion.payload_projection.queue_name`) and run by
`ProjectionWorkerSettings`, whose `max_jobs = 1` is what removes the Qdrant `sources[]`
read-modify-write race — see `apps/worker/settings.py`.
"""

from __future__ import annotations

from typing import Any

from graphrag.apps.worker.tasks._common import run_task
from graphrag.core.events import JobEnvelope, ProjectPayloadPayload
from graphrag.services.ingestion.service import ProjectionService


async def project_chunk_payload(
    ctx: dict[str, Any], env: JobEnvelope[ProjectPayloadPayload]
) -> None:
    """See `ingest_document`'s docstring for the contract shared by every task in this package.

    No `on_failure` ledger update here: unlike `ingest_document`/`delete_document`, this job is
    scoped to a batch of chunk_ids that may span many documents, not one `doc_id` — there is no
    single ledger row to mark FAILED.
    """
    container = ctx["container"]

    async def _body() -> None:
        service = ProjectionService(
            source_registry=container.sources, vector_store=container.vector_store
        )
        await service.project(env.payload.chunk_ids)

    await run_task("project_chunk_payload", ctx, env, _body)
