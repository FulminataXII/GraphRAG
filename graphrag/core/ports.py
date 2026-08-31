"""Protocol interfaces. See BLUEPRINT §3.5.

Protocols only. No implementations, no imports of I/O libraries. `core/` is the leaf layer:
every name referenced in an annotation below resolves to stdlib, pydantic, or another name
declared in `graphrag.core` — never `graphrag.adapters`, `graphrag.services`, `graphrag.config`,
or `graphrag.apps`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from graphrag.core.events import JobEnvelope
from graphrag.core.models import (
    Chunk,
    DocumentRecord,
    DocumentStatus,
    Entity,
    EntityType,
    GraphPath,
    JobStatus,
    Mention,
    Relation,
    ScoredChunk,
    SourceRef,
    SparseVector,
    StructuredResult,
)


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


@runtime_checkable
class IdGenerator(Protocol):
    def new_correlation_id(self) -> str: ...
    def new_job_id(self) -> str: ...


@runtime_checkable
class Embedder(Protocol):
    async def embed_dense(
        self, texts: Sequence[str], *, is_query: bool = False
    ) -> list[list[float]]: ...
    async def embed_sparse(self, texts: Sequence[str]) -> list[SparseVector]: ...
    @property
    def dimensions(self) -> int: ...


@runtime_checkable
class VectorStore(Protocol):
    async def ensure_collections(self) -> None:
        """Idempotent. MUST create the sparse vector config with the IDF modifier."""

    async def upsert_chunks(
        self, chunks: Sequence[Chunk], dense: Sequence[list[float]], sparse: Sequence[SparseVector]
    ) -> None: ...
    async def set_sources(self, chunk_id: UUID, sources: list[SourceRef]) -> None:
        """Overwrite the sources array wholesale. Caller supplies the authoritative list."""

    async def delete_chunks(self, chunk_ids: Sequence[UUID]) -> None: ...
    async def get_chunks(self, chunk_ids: Sequence[UUID]) -> list[Chunk]: ...
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
    ) -> list[ScoredChunk]: ...
    async def upsert_entities(
        self, entities: Sequence[Entity], vectors: Sequence[list[float]]
    ) -> None: ...
    async def search_entities(
        self, vector: list[float], *, top_k: int, entity_type: EntityType | None
    ) -> list[tuple[Entity, float]]: ...
    async def health(self) -> bool: ...


@runtime_checkable
class GraphStore(Protocol):
    async def ensure_schema(self) -> None:
        """Idempotent: constraints + indexes from ARCHITECTURE §6.3."""

    async def upsert_document(self, doc_id: str, uri: str, title: str, sha256: str) -> None: ...
    async def upsert_chunks(self, doc_id: str, chunks: Sequence[Chunk]) -> None:
        """Stores FULL chunk text (Chunk.text), not a preview."""

    async def upsert_entities(self, entities: Sequence[Entity]) -> None: ...
    async def upsert_relations(self, relations: Sequence[Relation]) -> None:
        """Rejects any relation with a null chunk_id or doc_id."""

    async def upsert_mentions(self, mentions: Sequence[Mention]) -> None:
        """Write (:Chunk)-[:MENTIONS {surface, confidence, char_start, char_end}]->(:Entity).

        Added BO-09 (BLUEPRINT §3.5). BO-08 shipped without it: `co_mentioned` and
        `top_entities_for_chunks` were implemented over `RELATES.chunk_id` alone, which only
        sees entities that participate in a relation. An entity the extractor found but linked
        to nothing is invisible to both templates — and entity linking in BO-09 needs exactly
        those. Rejects a mention whose `chunk_id` or `entity_id` is null (mirrors
        `upsert_relations`' provenance check). `Mention.entity_id` is unset by extraction and
        only populated by the resolve step — see `core.models.Mention`.
        """

    async def add_alias(
        self, alias_id: UUID, canonical_id: UUID, score: float, method: str
    ) -> None: ...
    async def traverse(
        self, template: str, params: dict[str, Any], *, timeout_ms: int
    ) -> list[GraphPath]: ...
    async def get_chunks(self, chunk_ids: Sequence[UUID]) -> list[Chunk]:
        """Hydrate full chunk text from the graph.

        Contract:
            - Returns Chunk objects with `text` AND `sources` populated. Sources are
              reconstructed from the (:Document)-[:HAS_CHUNK]->(:Chunk) edges, which is why
              HAS_CHUNK carries the offsets.
            - Returning text without sources would make every graph-path answer uncitable and
              would silently fail `verify_citations`, so this is not optional.
            - This is both the graph retrieval hydration path AND the vector-outage fallback.
        """

    async def delete_document(self, doc_id: str) -> None: ...
    async def health(self) -> bool: ...


@runtime_checkable
class LLMClient(Protocol):
    async def structured[T](
        self,
        *,
        role: str,
        messages: list[dict[str, str]],
        schema: type[T],
        max_repairs: int,
    ) -> StructuredResult[T]: ...
    async def stream_text(
        self, *, role: str, messages: list[dict[str, str]]
    ) -> AsyncIterator[str]: ...
    async def health(self) -> bool: ...


@runtime_checkable
class Cache(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, *, ttl_s: int) -> None: ...
    async def delete_prefix(self, prefix: str) -> int: ...


@runtime_checkable
class JobQueue(Protocol):
    async def enqueue(
        self,
        task: str,
        envelope: JobEnvelope[Any],
        *,
        job_id: str | None = None,
        queue_name: str | None = None,
    ) -> str: ...
    async def status(self, job_id: str) -> JobStatus: ...


@runtime_checkable
class DocumentLedger(Protocol):
    async def register(self, doc_id: str, uri: str, sha256: str, mime_type: str) -> bool:
        """Returns False if sha256 already present (duplicate upload)."""

    async def set_status(
        self, doc_id: str, status: DocumentStatus, error_code: str | None = None
    ) -> None: ...
    async def get(self, doc_id: str) -> DocumentRecord | None: ...
    async def bump_corpus_version(self) -> int: ...
    async def current_corpus_version(self) -> int: ...


@runtime_checkable
class SourceRegistry(Protocol):
    """Authoritative provenance store. Postgres PK(chunk_id, doc_id) arbitrates concurrency."""

    async def add(self, refs: Sequence[tuple[UUID, SourceRef]]) -> None:
        """INSERT ... ON CONFLICT (chunk_id, doc_id) DO NOTHING. Atomic, idempotent."""

    async def sources_for(self, chunk_ids: Sequence[UUID]) -> dict[UUID, list[SourceRef]]: ...
    async def remove_document(self, doc_id: str) -> list[UUID]:
        """Delete all rows for doc_id; return the affected chunk_ids for re-projection."""

    async def orphaned_chunks(self, chunk_ids: Sequence[UUID]) -> list[UUID]:
        """Subset with zero remaining source rows — safe to delete from both stores."""
