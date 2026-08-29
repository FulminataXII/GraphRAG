"""Trace context survives the real API -> Redis -> worker hop. See ARCHITECTURE §3.2 / BO-05.

Uses a REAL Redis-backed `ArqJobQueue` (proving the traceparent survives arq's own
serialization) and a REAL `arq.Worker` in burst mode (proving `apps/worker/tasks/_common.py`'s
`run_task` actually reconstructs the parent span from a job pulled off Redis by a separate
process-shaped consumer, not just a fake pool) — this is the `[integration]` counterpart to
`tests/unit/test_arq_queue.py`'s fake-pool version of the same mechanism.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from arq import Worker
from arq.connections import ArqRedis, RedisSettings, create_pool
from arq.worker import func as arq_func
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from graphrag.adapters.arq_queue import ArqJobQueue
from graphrag.apps.worker.tasks._common import run_task
from graphrag.core.events import IngestDocumentPayload, JobEnvelope

pytestmark = pytest.mark.integration

_REDIS_URL = "redis://localhost:6379/0"
_CAPTURED_TRACE_IDS: list[int] = []


@pytest.fixture
async def arq_pool() -> AsyncIterator[ArqRedis]:
    pool = await create_pool(RedisSettings.from_dsn(_REDIS_URL))
    yield pool
    await pool.aclose()


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


async def _record_trace_id_task(ctx: dict, env: JobEnvelope[IngestDocumentPayload]) -> None:
    async def _body() -> None:
        from opentelemetry import trace

        _CAPTURED_TRACE_IDS.append(trace.get_current_span().get_span_context().trace_id)

    await run_task("record_trace_id", ctx, env, _body)


async def test_trace_spans_queue_boundary(
    arq_pool: ArqRedis, span_exporter: InMemorySpanExporter
) -> None:
    _CAPTURED_TRACE_IDS.clear()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    tracer = provider.get_tracer("test-api")

    queue = ArqJobQueue(arq_pool)
    envelope = JobEnvelope(
        correlation_id="cid-trace-test",
        otel={},
        enqueued_at=datetime.now(UTC),
        payload=IngestDocumentPayload(
            doc_id="doc-trace", uri="file:///a.txt", sha256="sha", mime_type="text/plain"
        ),
    )

    with tracer.start_as_current_span("api-enqueue") as api_span:
        api_trace_id = api_span.get_span_context().trace_id
        job_id = f"trace-boundary-test-{uuid.uuid4().hex}"
        await queue.enqueue("record_trace_id", envelope, job_id=job_id)

    worker = Worker(
        functions=[arq_func(_record_trace_id_task, name="record_trace_id")],
        redis_pool=arq_pool,
        burst=True,
        handle_signals=False,
        max_jobs=1,
    )
    try:
        await worker.async_run()
    finally:
        await worker.close()

    assert _CAPTURED_TRACE_IDS, "worker never ran the enqueued job"
    assert _CAPTURED_TRACE_IDS[0] == api_trace_id, (
        "worker-side span must be a child of the API-side span (same trace_id)"
    )
