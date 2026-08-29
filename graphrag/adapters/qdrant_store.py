"""QdrantVectorStore — implements `core.ports.VectorStore`. See BLUEPRINT §5.2.

Owns two Qdrant collections:
    - `retrieval.vector.collection` ("chunks"): dense + BM25-sparse hybrid search over chunk
      text. The sparse vector MUST carry the IDF modifier — it cannot be added to an existing
      collection, only set at creation time.
    - `resolution.collection` ("entities"): a single dense vector (the same embedder, over
      entity names) used for kNN blocking during entity resolution (BO-07). Not detailed in
      BLUEPRINT §5.2's contract block, which only writes out the chunks collection's shape; the
      entities collection's shape (one "dense" vector, COSINE, no sparse leg, no payload
      indexes) is inferred from the `search_entities`/`upsert_entities` port methods and
      ARCHITECTURE's description of vector blocking. Flagged as a spec gap in the BO-04 report.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from uuid import UUID

from qdrant_client import models

from graphrag.core.errors import AppError, ConflictError, RetrievalBackendUnavailable
from graphrag.core.models import Chunk, Entity, EntityType, ScoredChunk, SourceRef, SparseVector

if TYPE_CHECKING:
    from qdrant_client import AsyncQdrantClient

    from graphrag.config.settings import Settings

_DENSE_VECTOR = "dense"
_SPARSE_VECTOR = "bm25"

# Field name -> Qdrant payload index type. BLUEPRINT §5.2: "Payload indexes: doc_ids (keyword),
# entity_ids (keyword), schema_version (integer)." The field NAMES are config-driven
# (`stores.qdrant.payload_indexes`); the TYPES are fixed, since the config only carries a
# list[str] of names, not a name->type mapping.
_PAYLOAD_INDEX_TYPES: dict[str, models.PayloadSchemaType] = {
    "doc_ids": models.PayloadSchemaType.KEYWORD,
    "entity_ids": models.PayloadSchemaType.KEYWORD,
    "schema_version": models.PayloadSchemaType.INTEGER,
}


class QdrantVectorStore:
    """Implements `VectorStore`. See BLUEPRINT §5.2.

    Contract:
        - `ensure_collections()` creates the chunks collection with:
            vectors_config={"dense": VectorParams(size=dim, distance=COSINE)}
            sparse_vectors_config={"bm25": SparseVectorParams(modifier=Modifier.IDF)}
          The IDF modifier is MANDATORY and cannot be added to an existing collection. If the
          collection exists WITHOUT it, raises `ConflictError` naming the required action.
        - Payload indexes: doc_ids (keyword), entity_ids (keyword), schema_version (integer).
          NOT `sources[].doc_id` — Qdrant's nested-array index/filter support is unreliable and
          orders of magnitude slower than a flat keyword match, so a flat `doc_ids` array is
          derived and indexed instead; `sources` stays un-indexed, rendering-only.
        - `hybrid_search` issues exactly ONE `query_points` call: two `Prefetch` branches (dense,
          bm25) fused via `models.RrfQuery(rrf=models.Rrf(k=rrf_k, weights=...))` — never
          `FusionQuery`, which accepts no k/weights.
        - An empty sparse vector is legal and must not raise; dense-only results are returned
          (Qdrant itself handles an empty sparse prefetch branch gracefully).
        - `set_sources` OVERWRITES the array via `set_payload` (patches only `sources` and the
          derived `doc_ids`); it never reads-then-appends. The caller (`ProjectionService`)
          supplies the authoritative list from Postgres.
        - Every library exception is wrapped in `RetrievalBackendUnavailable`; `ConflictError`
          raised deliberately by this adapter is never re-wrapped (see `_wrap_backend_errors`).
        - All calls carry a timeout from `stores.qdrant.timeout_s`.
    """

    def __init__(self, client: AsyncQdrantClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._chunks_collection = settings.retrieval.vector.collection
        self._entities_collection = settings.resolution.collection
        self._timeout_s = settings.stores.qdrant.timeout_s

    @asynccontextmanager
    async def _wrap_backend_errors(self, action: str) -> AsyncIterator[None]:
        try:
            yield
        except AppError:
            raise
        except Exception as exc:
            raise RetrievalBackendUnavailable(
                f"qdrant error during {action}", details={"action": action}
            ) from exc

    # -- schema -----------------------------------------------------------------------------

    async def ensure_collections(self) -> None:
        await self._ensure_chunks_collection()
        await self._ensure_entities_collection()

    async def _ensure_chunks_collection(self) -> None:
        name = self._chunks_collection
        async with self._wrap_backend_errors("ensure_collections(chunks)"):
            exists = await self._client.collection_exists(name)
            if exists:
                info = await self._client.get_collection(name)
                sparse_config = info.config.params.sparse_vectors or {}
                sparse_params = sparse_config.get(_SPARSE_VECTOR)
                if sparse_params is None or sparse_params.modifier != models.Modifier.IDF:
                    raise ConflictError(
                        f"Qdrant collection {name!r} exists without the IDF modifier on its "
                        f"{_SPARSE_VECTOR!r} sparse vector. The modifier cannot be added to an "
                        "existing collection — drop and recreate it (this re-indexes the whole "
                        "corpus). See BLUEPRINT §5.2.",
                        details={"collection": name},
                    )
                return

            dim = self._settings.embedding.dense.dimensions
            hnsw = self._settings.stores.qdrant.hnsw
            await self._client.create_collection(
                collection_name=name,
                vectors_config={
                    _DENSE_VECTOR: models.VectorParams(
                        size=dim,
                        distance=models.Distance.COSINE,
                        hnsw_config=models.HnswConfigDiff(m=hnsw.m, ef_construct=hnsw.ef_construct),
                    )
                },
                sparse_vectors_config={
                    _SPARSE_VECTOR: models.SparseVectorParams(modifier=models.Modifier.IDF)
                },
                timeout=self._timeout_s,
            )
            for field in self._settings.stores.qdrant.payload_indexes:
                await self._client.create_payload_index(
                    collection_name=name,
                    field_name=field,
                    field_schema=_PAYLOAD_INDEX_TYPES[field],
                    timeout=self._timeout_s,
                )

    async def _ensure_entities_collection(self) -> None:
        name = self._entities_collection
        async with self._wrap_backend_errors("ensure_collections(entities)"):
            if await self._client.collection_exists(name):
                return
            dim = self._settings.embedding.dense.dimensions
            await self._client.create_collection(
                collection_name=name,
                vectors_config={
                    _DENSE_VECTOR: models.VectorParams(size=dim, distance=models.Distance.COSINE)
                },
                timeout=self._timeout_s,
            )

    # -- chunks -------------------------------------------------------------------------------

    async def upsert_chunks(
        self, chunks: Sequence[Chunk], dense: Sequence[list[float]], sparse: Sequence[SparseVector]
    ) -> None:
        if not chunks:
            return
        async with self._wrap_backend_errors("upsert_chunks"):
            points = [
                self._chunk_to_point(chunk, dense_vec, sparse_vec)
                for chunk, dense_vec, sparse_vec in zip(chunks, dense, sparse, strict=True)
            ]
            await self._client.upsert(
                collection_name=self._chunks_collection, points=points, wait=True
            )

    async def set_sources(self, chunk_id: UUID, sources: list[SourceRef]) -> None:
        async with self._wrap_backend_errors("set_sources"):
            await self._client.set_payload(
                collection_name=self._chunks_collection,
                payload={
                    "sources": [ref.model_dump(mode="json") for ref in sources],
                    "doc_ids": _doc_ids(sources),
                },
                points=[str(chunk_id)],
                wait=True,
            )

    async def delete_chunks(self, chunk_ids: Sequence[UUID]) -> None:
        if not chunk_ids:
            return
        async with self._wrap_backend_errors("delete_chunks"):
            await self._client.delete(
                collection_name=self._chunks_collection,
                points_selector=models.PointIdsList(points=[str(cid) for cid in chunk_ids]),
                wait=True,
            )

    async def get_chunks(self, chunk_ids: Sequence[UUID]) -> list[Chunk]:
        if not chunk_ids:
            return []
        async with self._wrap_backend_errors("get_chunks"):
            records = await self._client.retrieve(
                collection_name=self._chunks_collection,
                ids=[str(cid) for cid in chunk_ids],
                with_payload=True,
                with_vectors=False,
            )
        return [self._payload_to_chunk(record.id, record.payload or {}) for record in records]

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
        query_filter = _build_filter(filters)
        rrf_weights = (
            [weights.get(_DENSE_VECTOR, 1.0), weights.get(_SPARSE_VECTOR, 1.0)] if weights else None
        )
        async with self._wrap_backend_errors("hybrid_search"):
            response = await self._client.query_points(
                collection_name=self._chunks_collection,
                prefetch=[
                    models.Prefetch(
                        query=dense,
                        using=_DENSE_VECTOR,
                        limit=prefetch_limit,
                        filter=query_filter,
                    ),
                    models.Prefetch(
                        query=models.SparseVector(indices=sparse.indices, values=sparse.values),
                        using=_SPARSE_VECTOR,
                        limit=prefetch_limit,
                        filter=query_filter,
                    ),
                ],
                query=models.RrfQuery(rrf=models.Rrf(k=rrf_k, weights=rrf_weights)),
                limit=top_k,
                with_payload=True,
                timeout=self._timeout_s,
            )
        return [
            ScoredChunk(
                chunk=self._payload_to_chunk(point.id, point.payload or {}),
                score=point.score,
                rank=rank,
                origin="fused",
            )
            for rank, point in enumerate(response.points, start=1)
        ]

    # -- entities -----------------------------------------------------------------------------

    async def upsert_entities(
        self, entities: Sequence[Entity], vectors: Sequence[list[float]]
    ) -> None:
        if not entities:
            return
        async with self._wrap_backend_errors("upsert_entities"):
            points = [
                models.PointStruct(
                    id=str(entity.canonical_id),
                    vector={_DENSE_VECTOR: vector},
                    payload=self._entity_payload(entity),
                )
                for entity, vector in zip(entities, vectors, strict=True)
            ]
            await self._client.upsert(
                collection_name=self._entities_collection, points=points, wait=True
            )

    async def search_entities(
        self, vector: list[float], *, top_k: int, entity_type: EntityType | None
    ) -> list[tuple[Entity, float]]:
        query_filter = None
        if entity_type is not None:
            query_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="type", match=models.MatchValue(value=entity_type.value)
                    )
                ]
            )
        async with self._wrap_backend_errors("search_entities"):
            response = await self._client.query_points(
                collection_name=self._entities_collection,
                query=vector,
                using=_DENSE_VECTOR,
                query_filter=query_filter,
                limit=top_k,
                with_payload=True,
                timeout=self._timeout_s,
            )
        return [
            (self._payload_to_entity(point.id, point.payload or {}), point.score)
            for point in response.points
        ]

    # -- health ---------------------------------------------------------------------------------

    async def health(self) -> bool:
        try:
            await self._client.get_collections()
            return True
        except Exception:
            return False

    # -- payload <-> domain model -----------------------------------------------------------

    def _chunk_to_point(
        self, chunk: Chunk, dense: list[float], sparse: SparseVector
    ) -> models.PointStruct:
        return models.PointStruct(
            id=str(chunk.chunk_id),
            vector={
                _DENSE_VECTOR: dense,
                _SPARSE_VECTOR: models.SparseVector(indices=sparse.indices, values=sparse.values),
            },
            payload={
                "text": chunk.text,
                "content_hash": chunk.content_hash,
                "sources": [ref.model_dump(mode="json") for ref in chunk.sources],
                "doc_ids": _doc_ids(chunk.sources),
                "entity_ids": [str(eid) for eid in chunk.entity_ids],
                "schema_version": chunk.schema_version,
            },
        )

    def _payload_to_chunk(self, point_id: Any, payload: dict[str, Any]) -> Chunk:
        return Chunk(
            chunk_id=UUID(str(point_id)),
            text=payload["text"],
            content_hash=payload["content_hash"],
            sources=[SourceRef(**ref) for ref in payload.get("sources", [])],
            entity_ids=[UUID(eid) for eid in payload.get("entity_ids", [])],
            schema_version=payload.get("schema_version", 1),
        )

    def _entity_payload(self, entity: Entity) -> dict[str, Any]:
        return {
            "name": entity.name,
            "name_normalized": entity.name_normalized,
            "type": entity.type.value,
            "aliases": entity.aliases,
            "mention_count": entity.mention_count,
        }

    def _payload_to_entity(self, point_id: Any, payload: dict[str, Any]) -> Entity:
        return Entity(
            canonical_id=UUID(str(point_id)),
            name=payload["name"],
            name_normalized=payload["name_normalized"],
            type=EntityType(payload["type"]),
            aliases=payload.get("aliases", []),
            mention_count=payload.get("mention_count", 0),
        )


def _doc_ids(sources: Sequence[SourceRef]) -> list[str]:
    """Flat, indexed, deduplicated doc_id array derived from `sources`. See the module
    docstring's payload-shape note (BLUEPRINT §5.2)."""
    seen: dict[str, None] = {}
    for ref in sources:
        seen[ref.doc_id] = None
    return list(seen)


def _build_filter(filters: dict[str, Any] | None) -> models.Filter | None:
    if not filters:
        return None
    conditions: list[models.FieldCondition] = []
    for key, value in filters.items():
        match = (
            models.MatchAny(any=value)
            if isinstance(value, list)
            else models.MatchValue(value=value)
        )
        conditions.append(models.FieldCondition(key=key, match=match))
    return models.Filter(must=conditions)
