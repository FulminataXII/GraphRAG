"""ArqJobQueue — implements `core.ports.JobQueue`. See BLUEPRINT §5.7.

`enqueue()` is where the API/worker boundary in ARCHITECTURE §3.2 is bridged: the W3C
traceparent is injected into `envelope.otel` here, before the envelope is serialized and handed
to arq, so `restore_context` on the worker side can re-attach the child span to the parent trace.
"""

from __future__ import annotations

from typing import Any, Protocol

from opentelemetry.context import Context
from opentelemetry.propagate import extract
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from graphrag.core.errors import ConflictError
from graphrag.core.events import JobEnvelope
from graphrag.core.models import JobStatus

#: arq's own Redis key prefixes for one job, in `arq.constants`. Duplicated here rather than
#: imported so this module keeps its single narrow dependency on arq (`arq.jobs`, imported
#: lazily in `status`); `drop_job` asserts nothing about them beyond what arq documents.
_JOB_KEY_PREFIXES: tuple[str, ...] = ("arq:job:", "arq:result:", "arq:retry:")


class _ArqRedisLike(Protocol):
    """The subset of `arq.connections.ArqRedis` this adapter needs.

    Protocol (not the concrete `arq.connections.ArqRedis` type) so unit tests can inject a
    hand-rolled fake pool without a real Redis server — see BLUEPRINT §9's "NO mocks, NO
    MagicMock" rule.
    """

    async def enqueue_job(
        self,
        function: str,
        *args: Any,
        _job_id: str | None = None,
        _queue_name: str | None = None,
        **kwargs: Any,
    ) -> Any: ...

    async def delete(self, *keys: str) -> Any: ...


def restore_context(envelope: JobEnvelope[Any]) -> Context:
    """Extract the parent OTel context from envelope.otel. Used by every task wrapper."""
    return extract(envelope.otel)


class ArqJobQueue:
    """Implements `JobQueue`.

    Contract (BLUEPRINT §5.7):
        - enqueue() injects the W3C traceparent into envelope.otel via
          TraceContextTextMapPropagator().inject BEFORE serializing.
        - Passes the envelope as the job's first positional argument. The trace carrier does
          NOT travel in arq's ctx dict.
        - job_id is the caller's idempotency key; arq's dedup on it is an optimisation, never
          the correctness mechanism (that's content-addressed IDs / MERGE / ON CONFLICT).
        - queue_name routes to the projection queue when specified.
        - enqueue() raises ConflictError when arq declined to queue the job because that job_id
          is already taken. The return type stays `str`: there is no "enqueued nothing"
          success value to hand back, so every returned id names a job that really exists.
    """

    def __init__(self, pool: _ArqRedisLike) -> None:
        self._pool = pool

    async def enqueue(
        self,
        task: str,
        envelope: JobEnvelope[Any],
        *,
        job_id: str | None = None,
        queue_name: str | None = None,
    ) -> str:
        carrier: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(carrier)
        enriched = envelope.model_copy(update={"otel": {**envelope.otel, **carrier}})

        job = await self._pool.enqueue_job(
            task,
            enriched,
            _job_id=job_id,
            _queue_name=queue_name,
        )
        # arq returns None when `arq:job:<id>` OR `arq:result:<id>` already exists, and it does
        # NOT enqueue anything in that case. This used to return `job_id` anyway, which reported
        # success for work that was never queued: `graphrag ingest` printed "enqueued ..." and
        # `DELETE /v1/documents/{id}` returned 202 for a deletion that never ran. The retained
        # RESULT key is the surprising half — arq keeps it for `keep_result` (3600s by default),
        # so for a full hour after a job finishes, re-enqueueing that same job_id silently does
        # nothing. The caller asked for work and got none; that is a failed request, not an
        # outcome to branch on.
        if job is None:
            raise ConflictError(
                f"job {job_id!r} was not enqueued: arq already holds a job or a retained "
                f"result under that id. arq keeps a finished job's result for its "
                f"`keep_result` window (3600s by default), so a re-run within the hour is "
                f"dropped silently. Clear it with `graphrag ingest --force <path>`, which "
                f"removes the retained keys and the ledger row before re-enqueueing.",
                details={"job_id": job_id, "task": task, "queue_name": queue_name},
            )
        resolved_id: str = job.job_id
        return resolved_id

    async def drop_job(self, job_id: str) -> None:
        """Delete arq's per-job keys so `job_id` can be enqueued again immediately.

        Deliberately NOT on the `core.ports.JobQueue` Protocol (BLUEPRINT §3.5): this is a
        recovery escape hatch for `graphrag ingest --force`, not part of the queue contract that
        services program against. Nothing in the normal ingest path may call it — clearing a
        live job's keys mid-flight would let a second copy of that job start alongside the
        first.
        """
        await self._pool.delete(*(prefix + job_id for prefix in _JOB_KEY_PREFIXES))

    async def status(self, job_id: str) -> JobStatus:
        from arq.jobs import Job as ArqJob
        from arq.jobs import JobStatus as ArqJobStatus

        arq_job = ArqJob(job_id, self._pool)  # type: ignore[arg-type]
        arq_status = await arq_job.status()
        info = await arq_job.info()
        result_info = await arq_job.result_info()

        state: str
        error: str | None = None
        if arq_status == ArqJobStatus.not_found:
            state = "not_found"
        elif arq_status == ArqJobStatus.deferred:
            state = "deferred"
        elif arq_status == ArqJobStatus.queued:
            state = "queued"
        elif arq_status == ArqJobStatus.in_progress:
            state = "in_progress"
        else:  # complete
            if result_info is not None and not result_info.success:
                state = "failed"
                error = str(result_info.result)
            else:
                state = "complete"

        return JobStatus(
            job_id=job_id,
            state=state,  # type: ignore[arg-type]
            attempts=(result_info.job_try if result_info else (info.job_try if info else 0)) or 0,
            enqueued_at=(info.enqueue_time if info else None),
            finished_at=(result_info.finish_time if result_info else None),
            error=error,
        )
