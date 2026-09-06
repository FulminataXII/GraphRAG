"""`ArqJobQueue` unit tests. See BLUEPRINT §5.7.

Uses a hand-rolled fake arq pool (BLUEPRINT §9: "NO mocks, NO MagicMock") that only implements
`enqueue_job` — enough to prove the traceparent-injection and positional-argument contracts
without a real Redis/arq worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from opentelemetry.context import attach, detach
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from graphrag.adapters.arq_queue import ArqJobQueue, restore_context
from graphrag.core.errors import ConflictError
from graphrag.core.events import INGEST_DOCUMENT, IngestDocumentPayload, JobEnvelope


@dataclass
class _FakeJobHandle:
    job_id: str


class _FakeArqPool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    async def delete(self, *keys: str) -> int:
        self.deleted.extend(keys)
        return len(keys)

    async def enqueue_job(
        self,
        function: str,
        *args: Any,
        _job_id: str | None = None,
        _queue_name: str | None = None,
        **kwargs: Any,
    ) -> _FakeJobHandle | None:
        self.calls.append(
            {"function": function, "args": args, "job_id": _job_id, "queue_name": _queue_name}
        )
        return _FakeJobHandle(job_id=_job_id or "generated-id")


class _CollidingArqPool(_FakeArqPool):
    """Simulates arq returning None: `arq:job:<id>` or `arq:result:<id>` already exists, so
    nothing was enqueued. The retained-RESULT case is the common one — arq keeps a finished
    job's result for `keep_result` (3600s by default), during which re-enqueueing that job_id
    is dropped silently."""

    async def enqueue_job(
        self,
        function: str,
        *args: Any,
        _job_id: str | None = None,
        _queue_name: str | None = None,
        **kwargs: Any,
    ) -> _FakeJobHandle | None:
        await super().enqueue_job(function, *args, _job_id=_job_id, _queue_name=_queue_name)
        return None


def _envelope() -> JobEnvelope[IngestDocumentPayload]:
    return JobEnvelope(
        correlation_id="cid-1",
        otel={},
        enqueued_at=datetime.now(UTC),
        payload=IngestDocumentPayload(
            doc_id="doc-1", uri="file:///a.txt", sha256="sha", mime_type="text/plain"
        ),
    )


async def test_enqueue_injects_traceparent() -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    pool = _FakeArqPool()
    queue = ArqJobQueue(pool)

    with tracer.start_as_current_span("enqueue-parent"):
        await queue.enqueue("ingest_document", _envelope(), job_id="idem-key-1")

    assert len(pool.calls) == 1
    enriched_envelope = pool.calls[0]["args"][0]
    assert "traceparent" in enriched_envelope.otel
    assert enriched_envelope.otel["traceparent"].startswith("00-")
    # Positional, first argument — never via arq's ctx dict.
    assert pool.calls[0]["job_id"] == "idem-key-1"
    assert pool.calls[0]["function"] == "ingest_document"


async def test_enqueue_raises_when_arq_queued_nothing() -> None:
    """A `None` from arq means NOTHING was enqueued, and must never be reported as success.

    This previously returned the caller's own `job_id`, on the reading that the idempotency key
    had "done its job". It had not: the caller asked for work and got none. `graphrag ingest`
    printed `enqueued ...` for a job that did not exist, and `DELETE /v1/documents/{id}`
    returned 202 for a deletion that never ran — for the full hour arq retains a finished job's
    result under that id.
    """
    queue = ArqJobQueue(_CollidingArqPool())
    with pytest.raises(ConflictError) as exc_info:
        await queue.enqueue(INGEST_DOCUMENT, _envelope(), job_id="idem-key-2")

    # The remedy is not guessable from the symptom, so the message has to carry it.
    message = exc_info.value.message
    assert "idem-key-2" in message
    assert "keep_result" in message
    assert "--force" in message
    assert exc_info.value.details["job_id"] == "idem-key-2"
    assert exc_info.value.details["task"] == INGEST_DOCUMENT


async def test_enqueue_returns_the_id_arq_actually_assigned() -> None:
    """The returned id names a job that really exists — arq's, not the caller's echo."""
    queue = ArqJobQueue(_FakeArqPool())
    assert await queue.enqueue(INGEST_DOCUMENT, _envelope()) == "generated-id"


async def test_drop_job_deletes_every_arq_key_for_that_id() -> None:
    """`graphrag ingest --force` depends on this: leaving `arq:result:<id>` behind is exactly
    what makes a forced re-enqueue silently do nothing."""
    pool = _FakeArqPool()
    await ArqJobQueue(pool).drop_job("doc-9")
    assert pool.deleted == ["arq:job:doc-9", "arq:result:doc-9", "arq:retry:doc-9"]


async def test_enqueue_routes_to_named_queue() -> None:
    pool = _FakeArqPool()
    queue = ArqJobQueue(pool)
    await queue.enqueue("project_payload", _envelope(), queue_name="projection")
    assert pool.calls[0]["queue_name"] == "projection"


async def test_restore_context_links_parent() -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    carrier: dict[str, str] = {}
    with tracer.start_as_current_span("parent") as parent_span:
        expected_trace_id = parent_span.get_span_context().trace_id
        TraceContextTextMapPropagator().inject(carrier)

    envelope = JobEnvelope(
        correlation_id="cid-2",
        otel=carrier,
        enqueued_at=datetime.now(UTC),
        payload=IngestDocumentPayload(
            doc_id="doc-2", uri="file:///b.txt", sha256="sha2", mime_type="text/plain"
        ),
    )

    parent_context = restore_context(envelope)
    token = attach(parent_context)
    try:
        with tracer.start_as_current_span("child") as child_span:
            child_trace_id = child_span.get_span_context().trace_id
    finally:
        detach(token)

    assert child_trace_id == expected_trace_id
