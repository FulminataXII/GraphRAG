"""`WorkerSettings`, `ProjectionWorkerSettings`. See BLUEPRINT §7.2.

Both classes read `get_settings()` at class-body (import) time — arq's own CLI (`arq
graphrag.apps.worker.settings.WorkerSettings`) loads them as plain module attributes, not
callables, which is the standard shape for an arq settings module. That makes THIS module
import-side-effecting (it needs `GRAPHRAG_*`/`.env` config to already be resolvable), which is
expected for a worker entrypoint but means tests that import it must set up config first, same
as `tests.conftest`'s `settings` fixture does for everything else.
"""

from __future__ import annotations

from typing import Any, ClassVar

from arq.connections import RedisSettings

from graphrag.adapters.telemetry.logging import configure_logging
from graphrag.adapters.telemetry.otel import init_telemetry, shutdown_telemetry
from graphrag.apps.api.main import Container
from graphrag.apps.worker.tasks.delete import delete_document
from graphrag.apps.worker.tasks.extract import extract_entities
from graphrag.apps.worker.tasks.ingest import ingest_document
from graphrag.apps.worker.tasks.project import project_chunk_payload
from graphrag.apps.worker.tasks.resolve import resolve_entities
from graphrag.config.settings import get_settings


async def _startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    init_telemetry(settings, service_role="worker")
    configure_logging(settings, service_role="worker")
    ctx["container"] = await Container.create(settings)


async def _shutdown(ctx: dict[str, Any]) -> None:
    container = ctx.get("container")
    if container is not None:
        await container.aclose()
    shutdown_telemetry()


class WorkerSettings:
    """arq worker configuration for the main ingest/delete queue.

    Contract:
        - functions = [ingest_document, extract_entities, resolve_entities, delete_document].
          BO-07 lands `extract_entities`/`resolve_entities` and registers both here — until now
          `IngestionService`'s `extract_entities` enqueue (BLUEPRINT §6.1 step 7) simply queued
          and waited, exactly like `Container.graph_store` staying `None` until BO-08 lands its
          real adapter (BO-03/04's precedent for landing a port before its BO).
        - on_startup builds the Container and stores it on ctx; on_shutdown closes it.
        - max_jobs = ingestion.parallelism.max_concurrent_docs
        - retry_jobs=True, max_tries from ingestion.dead_letter.max_attempts

    `health_check_interval` (arq's own default is 3600s) is set well below
    `docker-compose.yml`'s healthcheck `interval` for this service — arq writes its health
    sentinel to Redis on this cadence, and `arq --check` just reads it back; at the 3600s
    default the sentinel wouldn't exist yet for up to an hour after startup, so the container
    would sit in `starting`/flap between healthy and unhealthy forever.
    """

    functions: ClassVar[list[Any]] = [
        ingest_document,
        extract_entities,
        resolve_entities,
        delete_document,
    ]
    on_startup = staticmethod(_startup)
    on_shutdown = staticmethod(_shutdown)
    redis_settings = RedisSettings.from_dsn(get_settings().stores.redis.url)
    max_jobs = get_settings().ingestion.parallelism.max_concurrent_docs
    retry_jobs = True
    max_tries = get_settings().ingestion.dead_letter.max_attempts
    health_check_interval = 10


class ProjectionWorkerSettings:
    """SEPARATE worker process for the projection queue.

    Contract:
        - queue_name = ingestion.payload_projection.queue_name
        - max_jobs = 1. This single value is what removes the read-modify-write race;
          any value > 1 reintroduces it.

    `health_check_interval` — see `WorkerSettings`'s docstring; set well below
    `docker-compose.yml`'s healthcheck `interval` for the `projection-worker` service.
    """

    functions: ClassVar[list[Any]] = [project_chunk_payload]
    on_startup = staticmethod(_startup)
    on_shutdown = staticmethod(_shutdown)
    redis_settings = RedisSettings.from_dsn(get_settings().stores.redis.url)
    queue_name = get_settings().ingestion.payload_projection.queue_name
    max_jobs = 1
    health_check_interval = 10
