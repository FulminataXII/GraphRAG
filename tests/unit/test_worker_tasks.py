"""arq task wrapper unit tests. See BLUEPRINT §7.2 / BO-05."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from graphrag.apps.api.main import Container
from graphrag.apps.worker.tasks.ingest import ingest_document
from graphrag.core.events import SCHEMA_VERSION, IngestDocumentPayload, JobEnvelope
from graphrag.core.models import DocumentStatus


class _ExplodingContainer:
    """Any attribute access means the task actually tried to do real work — which a rejected
    envelope must never do."""

    def __getattr__(self, name: str) -> None:
        raise AssertionError(f"container.{name} should never be touched for a rejected envelope")


def _envelope(*, schema_version: int) -> JobEnvelope[IngestDocumentPayload]:
    return JobEnvelope(
        schema_version=schema_version,
        correlation_id="cid-1",
        otel={},
        enqueued_at=datetime.now(UTC),
        payload=IngestDocumentPayload(
            doc_id="doc-1", uri="file:///a.txt", sha256="sha", mime_type="text/plain"
        ),
    )


@pytest.mark.parametrize("schema_version", [SCHEMA_VERSION + 1, 0])
async def test_worker_rejects_wrong_schema_version(schema_version: int) -> None:
    ctx = {"container": _ExplodingContainer(), "job_id": "job-1", "job_try": 1}
    env = _envelope(schema_version=schema_version)

    # Must return cleanly — no exception, and (via _ExplodingContainer) no attempt to use the
    # container, which is what proves IngestionService was never reached.
    await ingest_document(ctx, env)


async def test_worker_accepts_matching_schema_version_and_proceeds() -> None:
    """Sanity check the rejection test isn't vacuous: a matching schema_version DOES reach the
    container (and therefore raises via `_ExplodingContainer`, proving the guard is bypassed)."""
    ctx = {"container": _ExplodingContainer(), "job_id": "job-1", "job_try": 1}
    env = _envelope(schema_version=SCHEMA_VERSION)

    with pytest.raises(AssertionError, match="should never be touched"):
        await ingest_document(ctx, env)


async def test_worker_ingest_document_reaches_real_ingestion_service(
    container: Container, tmp_path: Path
) -> None:
    """Exercises the REAL `IngestionService(...)` construction inside `ingest_document`, unlike
    the tests above which deliberately short-circuit via `_ExplodingContainer`. A missing/renamed
    constructor argument at that call site (e.g. the `metrics` kwarg `IngestionService` now
    requires) must fail here, since nothing else in this file's suite would catch it."""
    doc_path = tmp_path / "doc.txt"
    doc_path.write_text("hello world")
    uri = f"file://{doc_path}"

    await container.ledger.register("doc-1", uri, "sha", "text/plain")
    ctx = {"container": container, "job_id": "job-1", "job_try": 1}
    env = JobEnvelope(
        schema_version=SCHEMA_VERSION,
        correlation_id="cid-1",
        otel={},
        enqueued_at=datetime.now(UTC),
        payload=IngestDocumentPayload(
            doc_id="doc-1", uri=uri, sha256="sha", mime_type="text/plain"
        ),
    )

    await ingest_document(ctx, env)  # must not raise

    record = await container.ledger.get("doc-1")
    assert record is not None
    assert record.status != DocumentStatus.FAILED
