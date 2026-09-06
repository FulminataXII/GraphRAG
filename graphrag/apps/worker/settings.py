"""`WorkerSettings`, `ProjectionWorkerSettings`. See BLUEPRINT §7.2.

Both classes read `get_settings()` at class-body (import) time — arq's own CLI (`arq
graphrag.apps.worker.settings.WorkerSettings`) loads them as plain module attributes, not
callables, which is the standard shape for an arq settings module. That makes THIS module
import-side-effecting (it needs `GRAPHRAG_*`/`.env` config to already be resolvable), which is
expected for a worker entrypoint but means tests that import it must set up config first, same
as `tests.conftest`'s `settings` fixture does for everything else.

Task registration is EAGER, and deliberately so. §7.2 previously required bare import-string
paths in `functions` to keep this module's import chain lazy, but arq names a string-registered
function after THE STRING ITSELF (`arq/worker.py`'s `func()`: `name = name or coroutine`), so
every task registered as `graphrag.apps.worker.tasks.ingest.ingest_document` while every caller
enqueued `ingest_document`. Nothing matched: each job failed with `function '<name>' not found`
before any task code ran, and because that failure is arq-internal it never reached a task's
failure handler, so the document stayed at PENDING with no error recorded.

`func(path, name=...)` fixes the name, and resolves the import inside `func()` — i.e. here, at
module import, EARLIER than `Worker` construction. Measured on this codebase (median of 5 cold
imports): 959ms and 0 task modules with bare strings, 1527ms and 5 task modules this way. The
laziness that buys back protected `arq --check`, which reads only `redis_settings`,
`health_check_key` and `queue_name` and never builds a `Worker` — so +568ms lands against a 20s
compose healthcheck budget, 2.8% of a timeout that was never close to threatened. Correct
registration is worth more than that margin. §7.2's other half is unaffected: `Container`
construction still defers into `on_startup` below, and that is what the lazy-import requirement
was really protecting.
"""

from __future__ import annotations

from typing import Any, ClassVar

from arq.connections import RedisSettings
from arq.worker import func

from graphrag.adapters.telemetry.logging import configure_logging
from graphrag.adapters.telemetry.otel import init_telemetry, shutdown_telemetry
from graphrag.config.settings import get_settings
from graphrag.core.events import (
    DELETE_DOCUMENT,
    EXTRACT_ENTITIES,
    INGEST_DOCUMENT,
    PROJECT_CHUNK_PAYLOAD,
    RESOLVE_ENTITIES,
)


async def _startup(ctx: dict[str, Any]) -> None:
    from graphrag.apps.api.main import Container

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
        - functions = [ingest_document, extract_entities, resolve_entities, delete_document],
          each registered under the `core.events` name its callers enqueue with. The explicit
          `name=` is load-bearing, not decoration — see this module's docstring.
        - on_startup builds the Container and stores it on ctx; on_shutdown closes it.
        - max_jobs = ingestion.parallelism.max_concurrent_docs
        - job_timeout = ingestion.job_timeout_s
        - retry_jobs=True, max_tries from ingestion.dead_letter.max_attempts. Note that arq
          retries ONLY `arq.worker.Retry`, `RetryJob` and `asyncio.CancelledError`; a plain
          exception is a permanent failure on its first attempt regardless of `max_tries`. What
          makes `max_tries` mean anything for the failure that actually matters on a free tier
          is `tasks/_common.py` translating `RateLimited` into `Retry`.

    `health_check_interval` (arq's own default is 3600s) is set well below
    `docker-compose.yml`'s healthcheck `interval` for this service — arq writes its health
    sentinel to Redis on this cadence, and `arq --check` just reads it back; at the 3600s
    default the sentinel wouldn't exist yet for up to an hour after startup, so the container
    would sit in `starting`/flap between healthy and unhealthy forever.
    """

    functions: ClassVar[list[Any]] = [
        func("graphrag.apps.worker.tasks.ingest.ingest_document", name=INGEST_DOCUMENT),
        func("graphrag.apps.worker.tasks.extract.extract_entities", name=EXTRACT_ENTITIES),
        func("graphrag.apps.worker.tasks.resolve.resolve_entities", name=RESOLVE_ENTITIES),
        func("graphrag.apps.worker.tasks.delete.delete_document", name=DELETE_DOCUMENT),
    ]
    on_startup = staticmethod(_startup)
    on_shutdown = staticmethod(_shutdown)
    redis_settings = RedisSettings.from_dsn(get_settings().stores.redis.url)
    max_jobs = get_settings().ingestion.parallelism.max_concurrent_docs
    job_timeout = get_settings().ingestion.job_timeout_s
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

    functions: ClassVar[list[Any]] = [
        func(
            "graphrag.apps.worker.tasks.project.project_chunk_payload",
            name=PROJECT_CHUNK_PAYLOAD,
        )
    ]
    on_startup = staticmethod(_startup)
    on_shutdown = staticmethod(_shutdown)
    redis_settings = RedisSettings.from_dsn(get_settings().stores.redis.url)
    queue_name = get_settings().ingestion.payload_projection.queue_name
    max_jobs = 1
    job_timeout = get_settings().ingestion.job_timeout_s
    health_check_interval = 10
