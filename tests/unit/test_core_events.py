from __future__ import annotations

from datetime import UTC, datetime

from graphrag.core.events import IngestDocumentPayload, JobEnvelope


def test_job_envelope_json_roundtrip() -> None:
    payload = IngestDocumentPayload(
        doc_id="doc-1", uri="file:///doc-1.pdf", sha256="a" * 64, mime_type="application/pdf"
    )
    envelope = JobEnvelope[IngestDocumentPayload](
        correlation_id="01J8XABCDEFGHJKMNPQRSTVWXY",
        otel={"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"},
        enqueued_at=datetime(2024, 1, 1, tzinfo=UTC),
        payload=payload,
    )

    dumped = envelope.model_dump_json()
    restored = JobEnvelope[IngestDocumentPayload].model_validate_json(dumped)

    assert restored == envelope
