"""`IngestionService`, `ProjectionService`, `DeletionService`. See BLUEPRINT §6.1.

Two spec gaps this module works around (reported alongside the rest of the BO-05 build):

1. `IngestionService.ingest()`'s documented signature `(doc_id, raw, mime_type, uri)` names the
   business inputs but omits `correlation_id` — yet step 7 of its own contract ("Enqueue
   ProjectPayload ... and ExtractEntities") requires building a `JobEnvelope`, which has a
   required `correlation_id` field. `services/` cannot reach `adapters.telemetry.logging`'s
   contextvars (layering), so the only place this value can come from is an explicit parameter.
   Added as a required keyword-only argument.
2. `ProjectionService`'s contract says an orphaned chunk (0 remaining sources) is deleted "from
   VectorStore AND GraphStore", but `core.ports.GraphStore` exposes no per-chunk delete — only
   `delete_document(doc_id)`. This module deletes the orphan from `VectorStore` only; graph-side
   removal is `DeletionService`'s job via `GraphStore.delete_document`, which already runs
   whenever a whole document (and therefore all its exclusively-owned chunks) is removed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from uuid import UUID

from graphrag.core.events import (
    ExtractEntitiesPayload,
    JobEnvelope,
    ProjectPayloadPayload,
)
from graphrag.core.ids import chunk_id, content_hash
from graphrag.core.models import Chunk, DocumentStatus, SourceRef
from graphrag.services.ingestion.chunker import ChunkSpec, chunk_document

if TYPE_CHECKING:
    from graphrag.config.schema import IngestionSection
    from graphrag.core.ports import (
        Clock,
        DocumentLedger,
        Embedder,
        GraphStore,
        JobQueue,
        SourceRegistry,
        VectorStore,
    )
    from graphrag.services.ingestion.parser import DocumentParser

# Forward path through the document state machine that IngestionService/DeletionService may
# advance along. Mirrors `adapters.postgres.ledger._FORWARD` (BO-03) without importing it —
# services/ may not import adapters/; this is the ORDER, used only to decide whether a status
# transition has already happened (idempotent re-ingestion), never to validate legality —
# that's the ledger's own job, and it still raises ConflictError on an illegal move.
_STATUS_ORDER: tuple[DocumentStatus, ...] = (
    DocumentStatus.PENDING,
    DocumentStatus.PARSING,
    DocumentStatus.EMBEDDING,
    DocumentStatus.EXTRACTING,
    DocumentStatus.RESOLVING,
    DocumentStatus.INDEXED,
)


def document_sha256(raw: bytes) -> str:
    """sha256 hex of raw file bytes. Stored as `DocumentRecord.sha256`."""
    return hashlib.sha256(raw).hexdigest()


def document_id(raw: bytes) -> str:
    """Content-addressed `doc_id` from raw file bytes — same construction as `core.ids.chunk_id`
    (uuid5-shaped, version=5), but over the RAW bytes rather than normalized text, so byte-
    identical re-uploads always resolve to the same doc_id regardless of Idempotency-Key.

    `core/ids.py` (BO-01, locked) doesn't expose a `doc_id()` helper — only `chunk_id`/
    `entity_id`/... are in the BLUEPRINT §1a Type Index for that module — so this document-
    specific helper lives here instead of inventing a name in a module this BO doesn't own.
    """
    digest = hashlib.sha256(raw).digest()
    return str(UUID(bytes=digest[:16], version=5))


def _status_index(status: DocumentStatus | None) -> int:
    if status is None or status not in _STATUS_ORDER:
        return -1
    return _STATUS_ORDER.index(status)


class IngestionService:
    """Orchestrates one document through parse -> chunk -> embed -> store -> register sources.

    Contract of ingest(doc_id, raw, mime_type, uri, *, correlation_id):
        1. parse -> chunk -> compute chunk_id per chunk (content-addressed)
        2. Deduplicate chunk_ids WITHIN this document before any store call.
        3. Embed only chunk_ids not already present in the vector store.
        4. upsert_chunks for new chunks; existing chunks are left untouched.
        5. GraphStore.upsert_document + upsert_chunks (full text).
        6. SourceRegistry.add(...) — this is what makes multi-source retention work.
        7. Enqueue ProjectPayload for all touched chunk_ids, and ExtractEntities.
        8. Ledger status transitions at each step; bump_corpus_version at the end.
        - Idempotent: re-running for the same doc_id produces no duplicate chunks, no duplicate
          source rows, and no duplicate graph nodes.
        - It NEVER writes Qdrant's sources[] directly. That is ProjectionService's job.

    `graph_store` is `GraphStore | None` (not the bare `GraphStore` the constructor-dependency
    list names): as of BO-08, `Container.create()` always supplies a real `Neo4jGraphStore`, but
    the type stays Optional here since a caller (a unit test, or any future all-fakes fixture)
    may still legitimately construct this service without one. Step 5's graph write is skipped
    whenever `graph_store is None`.
    """

    def __init__(
        self,
        *,
        parser: DocumentParser,
        embedder: Embedder,
        vector_store: VectorStore,
        graph_store: GraphStore | None,
        source_registry: SourceRegistry,
        ledger: DocumentLedger,
        job_queue: JobQueue,
        clock: Clock,
        # `Metrics` (adapters.telemetry.metrics) has no `core.ports` Protocol — services/ may
        # not import adapters/ (layering), so this is typed `Any` rather than the concrete
        # class. Still DI, not a global: the caller wires the same `Metrics` instance used
        # everywhere else.
        metrics: Any,
        ingestion: IngestionSection,
    ) -> None:
        self._parser = parser
        self._embedder = embedder
        self._vector_store = vector_store
        self._graph_store = graph_store
        self._source_registry = source_registry
        self._ledger = ledger
        self._job_queue = job_queue
        self._clock = clock
        self._metrics = metrics
        self._ingestion = ingestion

    async def _advance(self, doc_id: str, target: DocumentStatus, current_idx: int) -> int:
        target_idx = _status_index(target)
        if target_idx <= current_idx:
            return current_idx
        await self._ledger.set_status(doc_id, target)
        return target_idx

    async def ingest(
        self, doc_id: str, raw: bytes, mime_type: str, uri: str, *, correlation_id: str
    ) -> None:
        record = await self._ledger.get(doc_id)
        current_idx = _status_index(record.status if record else None)

        current_idx = await self._advance(doc_id, DocumentStatus.PARSING, current_idx)
        parsed = self._parser.parse(raw, mime_type)
        specs = chunk_document(
            parsed,
            chunk_size=self._ingestion.chunk_size,
            chunk_overlap=self._ingestion.chunk_overlap,
            min_chunk_chars=self._ingestion.min_chunk_chars,
        )

        # Dedupe WITHIN this document — content addressing means two chunks of identical
        # normalized text share a chunk_id; the SourceRef identity note in core/models.py
        # (doc_id alone) means only the FIRST occurrence's offsets are worth keeping.
        by_id: dict[UUID, ChunkSpec] = {}
        for spec in specs:
            cid = chunk_id(spec.text)
            by_id.setdefault(cid, spec)
        all_chunk_ids = list(by_id.keys())

        current_idx = await self._advance(doc_id, DocumentStatus.EMBEDDING, current_idx)

        now = self._clock.now()
        chunks_by_id: dict[UUID, Chunk] = {
            cid: Chunk(
                chunk_id=cid,
                text=spec.text,
                content_hash=content_hash(spec.text),
                sources=[
                    SourceRef(
                        doc_id=doc_id,
                        uri=uri,
                        page=spec.page,
                        char_start=spec.char_start,
                        char_end=spec.char_end,
                        ingested_at=now,
                    )
                ],
                entity_ids=[],
            )
            for cid, spec in by_id.items()
        }

        existing = await self._vector_store.get_chunks(all_chunk_ids)
        existing_ids = {chunk.chunk_id for chunk in existing}
        new_ids = [cid for cid in all_chunk_ids if cid not in existing_ids]
        self._metrics.ingest_chunks_deduped.add(len(existing_ids))

        if new_ids:
            new_chunks = [chunks_by_id[cid] for cid in new_ids]
            texts = [chunk.text for chunk in new_chunks]
            dense = await self._embedder.embed_dense(texts)
            sparse = await self._embedder.embed_sparse(texts)
            await self._vector_store.upsert_chunks(new_chunks, dense, sparse)

        if self._graph_store is not None:
            title = parsed.title or uri
            await self._graph_store.upsert_document(doc_id, uri, title, sha256=document_sha256(raw))
            await self._graph_store.upsert_chunks(
                doc_id, [chunks_by_id[cid] for cid in all_chunk_ids]
            )

        refs = [(cid, chunks_by_id[cid].sources[0]) for cid in all_chunk_ids]
        await self._source_registry.add(refs)

        await self._job_queue.enqueue(
            "project_chunk_payload",
            JobEnvelope(
                correlation_id=correlation_id,
                otel={},
                enqueued_at=now,
                payload=ProjectPayloadPayload(chunk_ids=all_chunk_ids),
            ),
            queue_name=self._ingestion.payload_projection.queue_name,
        )
        await self._job_queue.enqueue(
            "extract_entities",
            JobEnvelope(
                correlation_id=correlation_id,
                otel={},
                enqueued_at=now,
                payload=ExtractEntitiesPayload(doc_id=doc_id, chunk_ids=all_chunk_ids),
            ),
        )

        await self._advance(doc_id, DocumentStatus.EXTRACTING, current_idx)
        await self._ledger.bump_corpus_version()


class ProjectionService:
    """Re-derives Qdrant sources[] from Postgres. Runs single-concurrency.

    Contract of project(chunk_ids):
        - reg.sources_for(chunk_ids) -> authoritative lists
        - For each chunk with >=1 source: VectorStore.set_sources(chunk_id, sources)
        - For each chunk with 0 sources: delete from VectorStore (see module docstring for the
          GraphStore per-chunk-delete port gap)
        - Convergent: running it twice yields the identical payload; running it over a
          hand-corrupted payload repairs it. This doubles as the reconciliation job.
        - Must be safe to run concurrently with ingestion (it only ever overwrites with truth).
    """

    def __init__(self, *, source_registry: SourceRegistry, vector_store: VectorStore) -> None:
        self._source_registry = source_registry
        self._vector_store = vector_store

    async def project(self, chunk_ids: Sequence[UUID]) -> None:
        if not chunk_ids:
            return
        sources_by_chunk = await self._source_registry.sources_for(chunk_ids)
        orphaned = set(await self._source_registry.orphaned_chunks(chunk_ids))

        for cid in chunk_ids:
            if cid in orphaned:
                continue
            sources = sources_by_chunk.get(cid, [])
            if sources:
                await self._vector_store.set_sources(cid, sources)

        if orphaned:
            await self._vector_store.delete_chunks(list(orphaned))


class DeletionService:
    """Contract of delete(doc_id):
        - SourceRegistry.remove_document(doc_id) -> affected chunk_ids
        - ProjectionService.project(affected)   (removes orphans from both stores)
        - GraphStore.delete_document(doc_id)
        - Ledger status -> DELETING; bump_corpus_version
        - Runs as a background job only. Never called from a request handler.

    `DocumentLedger` (BO-01/03, locked) has no row-removal method, only `set_status` — so
    "then removed" is realized as the terminal `DELETING` status (already the ledger's own
    terminal state; see `_FORWARD[DocumentStatus.DELETING] == frozenset()`), not a literal row
    delete. Flagged alongside the rest of this BO's spec gaps.
    """

    def __init__(
        self,
        *,
        source_registry: SourceRegistry,
        projection: ProjectionService,
        graph_store: GraphStore | None,
        ledger: DocumentLedger,
    ) -> None:
        self._source_registry = source_registry
        self._projection = projection
        self._graph_store = graph_store
        self._ledger = ledger

    async def delete(self, doc_id: str) -> None:
        affected = await self._source_registry.remove_document(doc_id)
        await self._projection.project(affected)
        if self._graph_store is not None:
            await self._graph_store.delete_document(doc_id)
        await self._ledger.set_status(doc_id, DocumentStatus.DELETING)
        await self._ledger.bump_corpus_version()


__all__ = [
    "DeletionService",
    "IngestionService",
    "ProjectionService",
    "document_id",
    "document_sha256",
]
