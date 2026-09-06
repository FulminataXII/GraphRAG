"""Shared per-task wrapper. See BLUEPRINT §7.2's "Contract (identical for all tasks)".

Not a BLUEPRINT-named component — a private helper factoring out the boilerplate common to
every task in this package (schema-version rejection, span/trace propagation across the queue
hop, correlation-id binding, retry policy, and cleanup) so `ingest.py`/`project.py`/`delete.py`
stay focused on their own business logic.

Two things here exist because of how arq's own failure handling actually behaves, which is
narrower than `retry_jobs=True` and `max_tries` suggest:

1. arq retries ONLY `arq.worker.Retry`, `RetryJob` and `asyncio.CancelledError`
   (`arq/worker.py`'s `run_job`). Every other exception is a permanent failure on its FIRST
   attempt, so `max_tries` was dead configuration for the failure most likely on a free tier:
   one 429 surviving LiteLLM's fallback chain ended a document permanently. `RateLimited` is
   therefore translated into `Retry` here, with a deferral taken from the provider's own
   `Retry-After` when it sent one.

2. `asyncio.CancelledError` inherits `BaseException`, so an `except Exception` handler never
   sees it. A job cancelled by arq's `job_timeout` therefore ran no failure handler at all and
   left its document stranded at whatever in-progress status it held, with nothing in the
   system able to detect or re-drive it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from arq.worker import Retry

from graphrag.adapters.arq_queue import restore_context
from graphrag.adapters.telemetry.logging import bind_request_context, clear_request_context
from graphrag.adapters.telemetry.otel import tracer
from graphrag.core.errors import JobTimeout, RateLimited
from graphrag.core.events import SCHEMA_VERSION, JobEnvelope

if TYPE_CHECKING:
    from graphrag.config.schema import AdaptiveRateLimitSpec

_log = logging.getLogger(__name__)


def _rate_limit_defer_s(
    spec: AdaptiveRateLimitSpec, *, retry_after: float | None, job_try: int
) -> float:
    """Seconds to defer a rate-limited job before its next attempt.

    Honours the provider's own `Retry-After` when it sent one and config says to
    (`llm.adaptive_rate_limit.honor_retry_after`), since a real reset time beats any guess we
    could make. Otherwise falls back to exponential backoff from `backoff_on_429_s`, doubling
    per attempt. Both are clamped to `max_backoff_s` — a provider may report a reset window
    longer than we are willing to hold a worker slot for, and an unbounded defer would park the
    job past the point anyone is still watching the ingest.

    These three keys were declared in config and read by nothing before this.
    """
    if spec.honor_retry_after and retry_after is not None:
        return min(float(retry_after), float(spec.max_backoff_s))
    backoff = spec.backoff_on_429_s * (2 ** max(job_try - 1, 0))
    return float(min(backoff, spec.max_backoff_s))


async def _record_failure(
    on_failure: Callable[[BaseException], Awaitable[None]] | None,
    exc: BaseException,
    *,
    shielded: bool = False,
) -> None:
    """Run a task's `on_failure` (which sets ledger status FAILED), never masking the original
    error if the handler itself fails.

    `shielded` is for the cancellation path: the task is already being torn down, so a bare
    `await` here could be interrupted before the ledger write lands and re-strand the very
    document this exists to record. `asyncio.shield` lets the write run to completion even if
    this coroutine is cancelled again while awaiting it — the same guarantee arq itself takes
    for `finish_job`.
    """
    if on_failure is None:
        return
    try:
        if shielded:
            await asyncio.shield(on_failure(exc))
        else:
            await on_failure(exc)
    except Exception:
        # A failure handler that itself fails (e.g. the ledger rejecting FAILED from a terminal
        # status, as `delete.py` documents) must not replace the real error with its own.
        _log.exception("on_failure handler raised while recording a task failure")


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
        - `RateLimited` is re-raised as `arq.worker.Retry`, deferred by the provider's
          `Retry-After` or the configured backoff, so arq actually retries it. On the LAST
          attempt it is instead treated as terminal, because a `Retry` raised there leads arq to
          fail the job internally with `max retries exceeded` — a path that never calls
          `on_failure`, which would strand the document exactly as before.
        - `LLMSchemaViolation` and `ValidationError` are deliberately NOT retried. They are
          deterministic: the same prompt and the same schema produce the same rejection, so a
          retry spends extraction quota to fail identically. `LiteLLMClient.structured` has
          already made `max_repairs` attempts at them before raising.
        - `asyncio.CancelledError` (arq's `job_timeout` firing) records status FAILED with code
          JOB_TIMEOUT and is re-raised IMMEDIATELY. It is never swallowed: suppressing a
          cancellation breaks the guarantee the event loop relies on and can wedge the worker.
        - On any other exception from `body`, `on_failure` (if given) runs first — this is
          where a task sets ledger status FAILED — then the exception is re-raised so arq
          records the failure and applies its own retry/dead-letter policy.

    Handler ordering is load-bearing. `CancelledError` gets its own clause because it inherits
    `BaseException` and `except Exception` cannot see it. `RateLimited` is caught before the
    general clause so a retryable 429 never reaches the handler that writes FAILED. And
    `clear_request_context()` stays in `finally`, which runs AFTER whichever handler matched —
    so the ledger write those handlers perform still emits its log lines with the correlation
    id, job_id and job_try bound.
    """
    if env.schema_version != SCHEMA_VERSION:
        _log.error(
            "rejecting envelope with incompatible schema_version",
            extra={"task": task_name, "got": env.schema_version, "expected": SCHEMA_VERSION},
        )
        return

    parent = restore_context(env)
    with tracer().start_as_current_span(task_name, context=parent):
        job_try = int(ctx.get("job_try", 1) or 1)
        bind_request_context(
            correlation_id=env.correlation_id,
            job_id=str(ctx.get("job_id", "")),
            job_try=str(job_try),
        )
        try:
            await body()
        except RateLimited as exc:
            settings = ctx["container"].settings
            max_tries = settings.ingestion.dead_letter.max_attempts
            if job_try >= max_tries:
                _log.error(
                    "rate limited on the final attempt; failing the document",
                    extra={"task": task_name, "job_try": job_try, "max_tries": max_tries},
                )
                await _record_failure(on_failure, exc)
                raise
            defer_s = _rate_limit_defer_s(
                settings.llm.adaptive_rate_limit,
                retry_after=exc.retry_after,
                job_try=job_try,
            )
            _log.warning(
                "rate limited; deferring retry",
                extra={
                    "task": task_name,
                    "job_try": job_try,
                    "max_tries": max_tries,
                    "defer_s": defer_s,
                    "retry_after": exc.retry_after,
                },
            )
            raise Retry(defer=defer_s) from exc
        except asyncio.CancelledError as exc:
            _log.error(
                "task cancelled before completion (arq job_timeout); marking the document "
                "FAILED so it is not left stranded at an in-progress status",
                extra={"task": task_name, "job_try": job_try},
            )
            await _record_failure(
                on_failure,
                JobTimeout(
                    f"{task_name} was cancelled before completion",
                    details={"task": task_name, "job_try": job_try},
                ),
                shielded=True,
            )
            raise exc
        except Exception as exc:
            await _record_failure(on_failure, exc)
            raise
        finally:
            clear_request_context()
