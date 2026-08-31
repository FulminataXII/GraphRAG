"""`resolve_entities` arq task. See BLUEPRINT §7.2 / §6.2.

Persists `ResolutionService.resolve()`'s output to the ONE sink BO-07 has: `VectorStore.
upsert_entities` (the Qdrant `entities` collection). Alias edges are computed
(`ResolutionResult.aliases`) but not written anywhere yet — `GraphStore.add_alias` is the
contracted sink for them and `Neo4jGraphStore` doesn't exist until BO-08, which is also where
"Wire graph writes into IngestionService / resolve task" is an explicit build step. This mirrors
the established pattern of a port staying unused/`None` until its owning BO lands the adapter
(e.g. `IngestionService`'s `graph_store: GraphStore | None`, BO-05/BO-08).
"""

from __future__ import annotations

from typing import Any

from graphrag.apps.worker.tasks._common import run_task
from graphrag.core.errors import AppError
from graphrag.core.events import JobEnvelope, ResolveEntitiesPayload
from graphrag.core.models import DocumentStatus
from graphrag.services.resolution.blocking import Blocker
from graphrag.services.resolution.service import ResolutionService


async def resolve_entities(ctx: dict[str, Any], env: JobEnvelope[ResolveEntitiesPayload]) -> None:
    """See `ingest_document`'s docstring for the contract shared by every task in this package."""
    container = ctx["container"]

    async def _on_failure(exc: BaseException) -> None:
        code = exc.code if isinstance(exc, AppError) else "INTERNAL_ERROR"
        await container.ledger.set_status(
            env.payload.doc_id, DocumentStatus.FAILED, error_code=code
        )

    async def _body() -> None:
        blocker = Blocker(
            vector_store=container.vector_store, resolution=container.settings.resolution
        )
        service = ResolutionService(
            blocker=blocker,
            embedder=container.embedder,
            resolution=container.settings.resolution,
            metrics=container.metrics,
        )
        result = await service.resolve(env.payload.mentions)

        if result.entities:
            vectors = await container.embedder.embed_dense(
                [entity.name_normalized for entity in result.entities]
            )
            await container.vector_store.upsert_entities(result.entities, vectors)

        # Forward-only, like `extract_entities`' own guard — see that module for why.
        record = await container.ledger.get(env.payload.doc_id)
        if record is not None and record.status == DocumentStatus.RESOLVING:
            await container.ledger.set_status(env.payload.doc_id, DocumentStatus.INDEXED)
            await container.ledger.bump_corpus_version()

    await run_task("resolve_entities", ctx, env, _body, on_failure=_on_failure)


__all__ = ["resolve_entities"]
