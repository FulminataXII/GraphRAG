"""arq task wrapper unit tests. See BLUEPRINT §7.2 / BO-05 / BO-07."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from graphrag.apps.api.main import Container
from graphrag.apps.worker.tasks.extract import extract_entities, extract_mentions
from graphrag.apps.worker.tasks.ingest import ingest_document
from graphrag.apps.worker.tasks.resolve import resolve_entities
from graphrag.core.events import (
    SCHEMA_VERSION,
    ExtractEntitiesPayload,
    IngestDocumentPayload,
    JobEnvelope,
    ResolveEntitiesPayload,
)
from graphrag.core.ids import chunk_id, content_hash
from graphrag.core.models import Chunk, DocumentStatus, EntityType, Mention, SparseVector
from graphrag.services.orchestration.schemas import EntityExtraction, MentionOut


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


def _extract_envelope(*, schema_version: int) -> JobEnvelope[ExtractEntitiesPayload]:
    return JobEnvelope(
        schema_version=schema_version,
        correlation_id="cid-1",
        otel={},
        enqueued_at=datetime.now(UTC),
        payload=ExtractEntitiesPayload(doc_id="doc-1", chunk_ids=[]),
    )


def _resolve_envelope(*, schema_version: int) -> JobEnvelope[ResolveEntitiesPayload]:
    return JobEnvelope(
        schema_version=schema_version,
        correlation_id="cid-1",
        otel={},
        enqueued_at=datetime.now(UTC),
        payload=ResolveEntitiesPayload(doc_id="doc-1", mentions=[]),
    )


@pytest.mark.parametrize("schema_version", [SCHEMA_VERSION + 1, 0])
async def test_extract_entities_rejects_wrong_schema_version(schema_version: int) -> None:
    ctx = {"container": _ExplodingContainer(), "job_id": "job-1", "job_try": 1}
    await extract_entities(ctx, _extract_envelope(schema_version=schema_version))


@pytest.mark.parametrize("schema_version", [SCHEMA_VERSION + 1, 0])
async def test_resolve_entities_rejects_wrong_schema_version(schema_version: int) -> None:
    ctx = {"container": _ExplodingContainer(), "job_id": "job-1", "job_try": 1}
    await resolve_entities(ctx, _resolve_envelope(schema_version=schema_version))


async def test_extract_entities_accepts_matching_schema_version_and_proceeds() -> None:
    ctx = {"container": _ExplodingContainer(), "job_id": "job-1", "job_try": 1}
    with pytest.raises(AssertionError, match="should never be touched"):
        await extract_entities(ctx, _extract_envelope(schema_version=SCHEMA_VERSION))


async def test_resolve_entities_accepts_matching_schema_version_and_proceeds() -> None:
    ctx = {"container": _ExplodingContainer(), "job_id": "job-1", "job_try": 1}
    with pytest.raises(AssertionError, match="should never be touched"):
        await resolve_entities(ctx, _resolve_envelope(schema_version=SCHEMA_VERSION))


def _make_chunk(text: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id(text),
        text=text,
        content_hash=content_hash(text),
        sources=[],
        entity_ids=[],
    )


async def test_extract_entities_reaches_real_extraction_and_enqueues_resolve(
    container: Container,
) -> None:
    """Exercises the REAL `extract_mentions()`/LLM-call path inside `extract_entities`, unlike
    the schema-version tests above which deliberately short-circuit via `_ExplodingContainer`."""
    chunk = _make_chunk("Acme Corp. announced a new product today.")
    await container.vector_store.upsert_chunks(
        [chunk], [[0.0] * 8], [SparseVector(indices=[], values=[])]
    )
    await container.ledger.register("doc-1", "file:///doc-1.txt", "sha", "text/plain")
    await container.ledger.set_status("doc-1", DocumentStatus.EXTRACTING)

    container.llm_client.script_structured(
        "bulk",
        EntityExtraction(
            entities=[
                MentionOut(
                    chunk_id=str(chunk.chunk_id),
                    surface="Acme Corp.",
                    type=EntityType.ORG,
                    char_start=0,
                    char_end=10,
                    confidence=0.95,
                )
            ],
            relations=[],
        ),
    )

    ctx = {"container": container, "job_id": "job-1", "job_try": 1}
    env = JobEnvelope(
        schema_version=SCHEMA_VERSION,
        correlation_id="cid-1",
        otel={},
        enqueued_at=datetime.now(UTC),
        payload=ExtractEntitiesPayload(doc_id="doc-1", chunk_ids=[chunk.chunk_id]),
    )

    await extract_entities(ctx, env)  # must not raise

    record = await container.ledger.get("doc-1")
    assert record is not None
    assert record.status == DocumentStatus.RESOLVING

    enqueued = [e for e in container.job_queue.enqueued if e["task"] == "resolve_entities"]
    assert len(enqueued) == 1
    resolve_payload: ResolveEntitiesPayload = enqueued[0]["envelope"].payload
    assert resolve_payload.doc_id == "doc-1"
    assert len(resolve_payload.mentions) == 1
    assert resolve_payload.mentions[0].surface == "Acme Corp."


async def test_extraction_spans_within_chunk() -> None:
    """Every mention's char_start/char_end lies inside its claimed chunk's text; a mention
    claiming an out-of-bounds span or an unknown chunk_id is dropped, not passed through."""
    chunk = _make_chunk("short")  # len 5

    class _ScriptedLLM:
        async def structured(self, *, role, messages, schema, max_repairs):
            from graphrag.core.models import StructuredResult

            return StructuredResult(
                value=EntityExtraction(
                    entities=[
                        MentionOut(  # valid: 0 <= start < end <= len("short")==5
                            chunk_id=str(chunk.chunk_id),
                            surface="short",
                            type=EntityType.ORG,
                            char_start=0,
                            char_end=5,
                            confidence=0.9,
                        ),
                        MentionOut(  # invalid: end exceeds chunk length
                            chunk_id=str(chunk.chunk_id),
                            surface="short!!",
                            type=EntityType.ORG,
                            char_start=0,
                            char_end=50,
                            confidence=0.9,
                        ),
                        MentionOut(  # invalid: hallucinated chunk_id, not in this batch
                            chunk_id=str(uuid4()),
                            surface="ghost",
                            type=EntityType.ORG,
                            char_start=0,
                            char_end=5,
                            confidence=0.9,
                        ),
                    ],
                    relations=[],
                ),
                model_served="fake",
                repair_attempts=0,
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=0,
            )

    mentions = await extract_mentions(
        [chunk], llm_client=_ScriptedLLM(), batch_size=20, max_repairs=2
    )

    assert len(mentions) == 1
    assert isinstance(mentions[0], Mention)
    assert mentions[0].char_start >= 0
    assert mentions[0].char_end <= len(chunk.text)


async def test_extract_mentions_batches_at_configured_size() -> None:
    """N chunks over batch_size B make ceil(N/B) LLM calls, not N."""
    chunks = [_make_chunk(f"chunk number {i}") for i in range(45)]

    class _CountingLLM:
        def __init__(self) -> None:
            self.call_count = 0

        async def structured(self, *, role, messages, schema, max_repairs):
            from graphrag.core.models import StructuredResult

            self.call_count += 1
            assert role == "bulk"
            return StructuredResult(
                value=EntityExtraction(entities=[], relations=[]),
                model_served="fake",
                repair_attempts=0,
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=0,
            )

    llm = _CountingLLM()
    await extract_mentions(chunks, llm_client=llm, batch_size=20, max_repairs=2)
    assert llm.call_count == 3  # 45 chunks / 20 per request = ceil(2.25) = 3


async def test_resolve_entities_reaches_real_resolution_and_persists_entities(
    container: Container,
) -> None:
    """Exercises the REAL `ResolutionService`/`Blocker` construction and Qdrant `entities`
    persistence inside `resolve_entities`."""
    await container.ledger.register("doc-1", "file:///doc-1.txt", "sha", "text/plain")
    await container.ledger.set_status("doc-1", DocumentStatus.EXTRACTING)
    await container.ledger.set_status("doc-1", DocumentStatus.RESOLVING)

    mention = Mention(
        surface="Acme Corp.",
        type=EntityType.ORG,
        chunk_id=uuid4(),
        char_start=0,
        char_end=10,
        confidence=0.9,
    )

    ctx = {"container": container, "job_id": "job-1", "job_try": 1}
    env = JobEnvelope(
        schema_version=SCHEMA_VERSION,
        correlation_id="cid-1",
        otel={},
        enqueued_at=datetime.now(UTC),
        payload=ResolveEntitiesPayload(doc_id="doc-1", mentions=[mention]),
    )

    await resolve_entities(ctx, env)  # must not raise

    record = await container.ledger.get("doc-1")
    assert record is not None
    assert record.status == DocumentStatus.INDEXED

    assert len(container.vector_store.entities) == 1
    (persisted,) = container.vector_store.entities.values()
    assert persisted.name == "Acme Corp."
