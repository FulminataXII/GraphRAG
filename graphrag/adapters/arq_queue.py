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

from graphrag.core.events import JobEnvelope
from graphrag.core.models import JobStatus


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
        # arq returns None when _job_id collides with a still-queued/retained job — the
        # idempotency key is doing exactly its job, so surface it rather than raising.
        resolved_id = job.job_id if job is not None else job_id
        assert resolved_id is not None
        return resolved_id

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
