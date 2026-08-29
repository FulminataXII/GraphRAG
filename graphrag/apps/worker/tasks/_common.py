"""Shared per-task wrapper. See BLUEPRINT §7.2's "Contract (identical for all tasks)".

Not a BLUEPRINT-named component — a private helper factoring out the boilerplate common to
every task in this package (schema-version rejection, span/trace propagation across the queue
hop, correlation-id binding, and cleanup) so `ingest.py`/`project.py`/`delete.py` stay focused
on their own business logic.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from graphrag.adapters.arq_queue import restore_context
from graphrag.adapters.telemetry.logging import bind_request_context, clear_request_context
from graphrag.adapters.telemetry.otel import tracer
from graphrag.core.events import SCHEMA_VERSION, JobEnvelope

_log = logging.getLogger(__name__)


async def run_task[P](
    task_name: str,
    ctx: dict[str, Any],
    env: JobEnvelope[P],
    body: Callable[[], Awaitable[None]],
    *,
    on_failure: Callable[[BaseException], Awaitable[None]] | None = None,
) -> None:
    """Runs `body()` inside a span that's a child of the enqueue-side trace, with the
    correlation id (and arq's own job_id/job_try) bound for every log line `body` emits.

    Contract:
        - An envelope whose schema_version differs from SCHEMA_VERSION is rejected: logged and
          dropped WITHOUT raising (retrying a version mismatch can never succeed, so letting arq
          retry it up to `max_tries` and dead-letter it would be pure waste).
        - On any other exception from `body`, `on_failure` (if given) runs first — this is
          where a task sets ledger status FAILED — then the exception is re-raised so arq
          records the failure and applies its own retry/dead-letter policy.
    """
    if env.schema_version != SCHEMA_VERSION:
        _log.error(
            "rejecting envelope with incompatible schema_version",
            extra={"task": task_name, "got": env.schema_version, "expected": SCHEMA_VERSION},
        )
        return

    parent = restore_context(env)
    with tracer().start_as_current_span(task_name, context=parent):
        bind_request_context(
            correlation_id=env.correlation_id,
            job_id=str(ctx.get("job_id", "")),
            job_try=str(ctx.get("job_try", "")),
        )
        try:
            await body()
        except Exception as exc:
            if on_failure is not None:
                await on_failure(exc)
            raise
        finally:
            clear_request_context()
