"""In-memory implementations of every port. NO mocks, NO MagicMock. See BLUEPRINT §9.

Each Fake implements the corresponding `core.ports` Protocol structurally (duck typing — no
inheritance declared), which is what `test_fakes_satisfy_protocols` checks via `isinstance()`
against the `@runtime_checkable` Protocols.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from graphrag.core.errors import (
    GraphBackendUnavailable,
    LLMProviderExhausted,
    LLMSchemaViolation,
    RetrievalBackendUnavailable,
)
from graphrag.core.models import (
    Chunk,
    DocumentRecord,
    DocumentStatus,
    Entity,
    EntityType,
    GraphPath,
    JobStatus,
    Relation,
    ScoredChunk,
    SourceRef,
    SparseVector,
    StructuredResult,
)


class FakeClock:
    """Settable; advance(seconds)."""

    def __init__(self, initial: datetime | None = None) -> None:
        self._now = initial or datetime(2024, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class FakeIdGenerator:
    """Deterministic counter — no real randomness, so test output is reproducible."""

    def __init__(self) -> None:
        self._correlation_counter = 0
        self._job_counter = 0

    def new_correlation_id(self) -> str:
        self._correlation_counter += 1
        return f"test-correlation-{self._correlation_counter:06d}"

    def new_job_id(self) -> str:
        self._job_counter += 1
        return f"test-job-{self._job_counter:06d}"


class FakeEmbedder:
    """Hash-derived deterministic vectors — same text always embeds to the same vector."""

    def __init__(self, dimensions: int = 8) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_dense(
        self, texts: Sequence[str], *, is_query: bool = False
    ) -> list[list[float]]:
        return [self._dense_vector(text) for text in texts]

    async def embed_sparse(self, texts: Sequence[str]) -> list[SparseVector]:
        return [self._sparse_vector(text) for text in texts]

    def _dense_vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [b / 255 for b in digest[: self._dimensions]]

    def _sparse_vector(self, text: str) -> SparseVector:
        words = text.split()
        if not words:
            return SparseVector(indices=[], values=[])
        counts: dict[int, float] = {}
        for word in words:
            idx = int(hashlib.sha256(word.encode("utf-8")).hexdigest(), 16) % 1000
            counts[idx] = counts.get(idx, 0.0) + 1.0
        indices = sorted(counts)
        return SparseVector(indices=indices, values=[counts[i] for i in indices])


class FakeVectorStore:
    """Dict-backed; set `.fail = True` to make every method raise like a dead backend."""

    def __init__(self) -> None:
        self.chunks: dict[UUID, Chunk] = {}
        self.dense: dict[UUID, list[float]] = {}
        self.sparse: dict[UUID, SparseVector] = {}
        self.entities: dict[UUID, Entity] = {}
        self.entity_vectors: dict[UUID, list[float]] = {}
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            raise RetrievalBackendUnavailable("FakeVectorStore is configured to fail")

    async def ensure_collections(self) -> None:
        self._check()

    async def upsert_chunks(
        self, chunks: Sequence[Chunk], dense: Sequence[list[float]], sparse: Sequence[SparseVector]
    ) -> None:
        self._check()
        for chunk, dense_vec, sparse_vec in zip(chunks, dense, sparse, strict=True):
            self.chunks[chunk.chunk_id] = chunk
            self.dense[chunk.chunk_id] = dense_vec
            self.sparse[chunk.chunk_id] = sparse_vec

    async def set_sources(self, chunk_id: UUID, sources: list[SourceRef]) -> None:
        self._check()
        chunk = self.chunks[chunk_id]
        self.chunks[chunk_id] = chunk.model_copy(update={"sources": sources})

    async def delete_chunks(self, chunk_ids: Sequence[UUID]) -> None:
        self._check()
        for chunk_id in chunk_ids:
            self.chunks.pop(chunk_id, None)
            self.dense.pop(chunk_id, None)
            self.sparse.pop(chunk_id, None)

    async def get_chunks(self, chunk_ids: Sequence[UUID]) -> list[Chunk]:
        self._check()
        return [self.chunks[cid] for cid in chunk_ids if cid in self.chunks]

    async def hybrid_search(
        self,
        *,
        dense: list[float],
        sparse: SparseVector,
        top_k: int,
        prefetch_limit: int,
        rrf_k: int,
        weights: dict[str, float] | None,
        filters: dict[str, Any] | None = None,
    ) -> list[ScoredChunk]:
        self._check()
        return [
            ScoredChunk(chunk=chunk, score=1.0, rank=rank, origin="vector")
            for rank, chunk in enumerate(list(self.chunks.values())[:top_k], start=1)
        ]

    async def upsert_entities(
        self, entities: Sequence[Entity], vectors: Sequence[list[float]]
    ) -> None:
        self._check()
        for entity, vector in zip(entities, vectors, strict=True):
            self.entities[entity.canonical_id] = entity
            self.entity_vectors[entity.canonical_id] = vector

    async def search_entities(
        self, vector: list[float], *, top_k: int, entity_type: EntityType | None
    ) -> list[tuple[Entity, float]]:
        self._check()
        matches = [
            entity
            for entity in self.entities.values()
            if entity_type is None or entity.type == entity_type
        ]
        return [(entity, 1.0) for entity in matches[:top_k]]

    async def health(self) -> bool:
        return not self.fail


class FakeGraphStore:
    """Dict-backed; set `.fail = True` to make every method raise like a dead backend."""

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, str]] = {}
        self.doc_chunks: dict[str, list[UUID]] = {}
        self.chunks: dict[UUID, Chunk] = {}
        self.entities: dict[UUID, Entity] = {}
        self.relations: list[Relation] = []
        self.aliases: dict[UUID, UUID] = {}
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            raise GraphBackendUnavailable("FakeGraphStore is configured to fail")

    async def ensure_schema(self) -> None:
        self._check()

    async def upsert_document(self, doc_id: str, uri: str, title: str, sha256: str) -> None:
        self._check()
        self.documents[doc_id] = {"uri": uri, "title": title, "sha256": sha256}

    async def upsert_chunks(self, doc_id: str, chunks: Sequence[Chunk]) -> None:
        self._check()
        seen = self.doc_chunks.setdefault(doc_id, [])
        for chunk in chunks:
            self.chunks[chunk.chunk_id] = chunk
            if chunk.chunk_id not in seen:
                seen.append(chunk.chunk_id)

    async def upsert_entities(self, entities: Sequence[Entity]) -> None:
        self._check()
        for entity in entities:
            self.entities[entity.canonical_id] = entity

    async def upsert_relations(self, relations: Sequence[Relation]) -> None:
        self._check()
        self.relations.extend(relations)

    async def add_alias(
        self, alias_id: UUID, canonical_id: UUID, score: float, method: str
    ) -> None:
        self._check()
        self.aliases[alias_id] = canonical_id

    async def traverse(
        self, template: str, params: dict[str, Any], *, timeout_ms: int
    ) -> list[GraphPath]:
        self._check()
        return []

    async def get_chunks(self, chunk_ids: Sequence[UUID]) -> list[Chunk]:
        self._check()
        return [self.chunks[cid] for cid in chunk_ids if cid in self.chunks]

    async def delete_document(self, doc_id: str) -> None:
        self._check()
        for chunk_id in self.doc_chunks.pop(doc_id, []):
            self.chunks.pop(chunk_id, None)
        self.documents.pop(doc_id, None)

    async def health(self) -> bool:
        return not self.fail


class FakeLLMClient:
    """Scripted responses queue; can emit invalid JSON N times.

    `script_structured(role, *items)` queues return values for a role. An item that is an
    `Exception` instance simulates a repair-triggering failure (e.g. a schema violation) —
    the next queued item is then returned/raised as the "repaired" attempt.
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.fail = False

    def script_structured(self, role: str, *responses: Any) -> None:
        self._queues.setdefault(role, []).extend(responses)

    async def structured[T](
        self,
        *,
        role: str,
        messages: list[dict[str, str]],
        schema: type[T],
        max_repairs: int,
    ) -> StructuredResult[T]:
        self.calls.append({"role": role, "messages": messages, "schema": schema})
        if self.fail:
            raise LLMProviderExhausted("FakeLLMClient is configured to fail")

        queue = self._queues.get(role)
        if not queue:
            raise LLMProviderExhausted(f"FakeLLMClient has no scripted response for role={role}")

        repairs = 0
        while queue:
            item = queue.pop(0)
            if isinstance(item, Exception):
                repairs += 1
                if repairs > max_repairs:
                    raise LLMSchemaViolation(
                        f"FakeLLMClient exhausted scripted responses for role={role}"
                    )
                continue
            return StructuredResult(
                value=item,
                model_served=f"fake-{role}",
                repair_attempts=repairs,
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=0,
            )
        raise LLMSchemaViolation(f"FakeLLMClient exhausted scripted responses for role={role}")

    async def stream_text(self, *, role: str, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        for token in ("fake", " ", "stream"):
            yield token

    async def health(self) -> bool:
        return not self.fail


class FakeCache:
    """Dict-backed; set `.fail = True` to simulate a degraded (never-raising) Redis outage."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self.fail = False

    async def get(self, key: str) -> bytes | None:
        if self.fail:
            return None
        return self._store.get(key)

    async def set(self, key: str, value: bytes, *, ttl_s: int) -> None:
        if self.fail:
            return
        self._store[key] = value

    async def delete_prefix(self, prefix: str) -> int:
        if self.fail:
            return 0
        matched = [key for key in self._store if key.startswith(prefix)]
        for key in matched:
            del self._store[key]
        return len(matched)


class FakeJobQueue:
    def __init__(self, id_generator: FakeIdGenerator | None = None) -> None:
        self._id_generator = id_generator or FakeIdGenerator()
        self.enqueued: list[dict[str, Any]] = []
        self._status: dict[str, JobStatus] = {}

    async def enqueue(
        self,
        task: str,
        envelope: Any,
        *,
        job_id: str | None = None,
        queue_name: str | None = None,
    ) -> str:
        resolved_id = job_id or self._id_generator.new_job_id()
        self.enqueued.append(
            {"task": task, "envelope": envelope, "job_id": resolved_id, "queue_name": queue_name}
        )
        self._status[resolved_id] = JobStatus(
            job_id=resolved_id,
            state="queued",
            attempts=0,
            enqueued_at=None,
            finished_at=None,
            error=None,
        )
        return resolved_id

    async def status(self, job_id: str) -> JobStatus:
        return self._status.get(job_id) or JobStatus(
            job_id=job_id,
            state="not_found",
            attempts=0,
            enqueued_at=None,
            finished_at=None,
            error=None,
        )

    def set_status(self, job_id: str, status: JobStatus) -> None:
        self._status[job_id] = status


class FakeSourceRegistry:
    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, str], SourceRef] = {}
        self.fail = False

    async def add(self, refs: Sequence[tuple[UUID, SourceRef]]) -> None:
        if self.fail:
            raise RetrievalBackendUnavailable("FakeSourceRegistry is configured to fail")
        for chunk_id, ref in refs:
            self._rows.setdefault((chunk_id, ref.doc_id), ref)

    async def sources_for(self, chunk_ids: Sequence[UUID]) -> dict[UUID, list[SourceRef]]:
        result: dict[UUID, list[SourceRef]] = {chunk_id: [] for chunk_id in chunk_ids}
        for (chunk_id, _doc_id), ref in self._rows.items():
            if chunk_id in result:
                result[chunk_id].append(ref)
        return result

    async def remove_document(self, doc_id: str) -> list[UUID]:
        affected = [chunk_id for (chunk_id, d) in list(self._rows) if d == doc_id]
        for chunk_id in affected:
            del self._rows[(chunk_id, doc_id)]
        return affected

    async def orphaned_chunks(self, chunk_ids: Sequence[UUID]) -> list[UUID]:
        remaining = {chunk_id for (chunk_id, _doc_id) in self._rows}
        return [chunk_id for chunk_id in chunk_ids if chunk_id not in remaining]


class FakeDocumentLedger:
    def __init__(self, clock: FakeClock | None = None) -> None:
        self._clock = clock or FakeClock()
        self._records: dict[str, DocumentRecord] = {}
        self._by_sha256: dict[str, str] = {}
        self._corpus_version = 0

    async def register(self, doc_id: str, uri: str, sha256: str, mime_type: str) -> bool:
        if sha256 in self._by_sha256:
            return False
        now = self._clock.now()
        self._records[doc_id] = DocumentRecord(
            doc_id=doc_id,
            uri=uri,
            sha256=sha256,
            mime_type=mime_type,
            status=DocumentStatus.PENDING,
            error_code=None,
            attempts=0,
            corpus_version=self._corpus_version,
            created_at=now,
            updated_at=now,
        )
        self._by_sha256[sha256] = doc_id
        return True

    async def set_status(
        self, doc_id: str, status: DocumentStatus, error_code: str | None = None
    ) -> None:
        record = self._records[doc_id]
        self._records[doc_id] = record.model_copy(
            update={"status": status, "error_code": error_code, "updated_at": self._clock.now()}
        )

    async def get(self, doc_id: str) -> DocumentRecord | None:
        return self._records.get(doc_id)

    async def bump_corpus_version(self) -> int:
        self._corpus_version += 1
        return self._corpus_version

    async def current_corpus_version(self) -> int:
        return self._corpus_version
