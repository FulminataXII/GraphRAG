"""`EntityLinker` — maps free-text query mentions to canonical entities. See BLUEPRINT §6.3."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from graphrag.core.models import Entity
    from graphrag.core.ports import Embedder, VectorStore


class EntityLinker:
    """Implements `GraphRetriever`'s entity-linking step (BLUEPRINT §6.3).

    Contract of link(query, top_k, min_score) -> list[Entity]:
        - Embeds candidate spans, kNN against the entities collection.
        - Drops matches below `min_score`.
        - Returns [] when nothing matches. A miss is NOT an error.

    JUDGMENT CALL — "candidate spans": nothing in BO-09's scope (BLUEPRINT §6.3 names only
    `linker.py`, `vector.py`, `graph.py`, `fusion.py`) builds a span extractor, and `link()`'s
    own signature takes one `query: str`, not a list of spans. `query` is embedded whole as the
    one candidate span — callers that already have distinct seed strings (e.g.
    `GraphRetriever`, working from `RoutePlan.seed_entities`) call `link()` once per string
    instead. Building a sub-string span extractor here would be inventing scope this BO wasn't
    asked for.
    """

    def __init__(self, *, embedder: Embedder, vector_store: VectorStore) -> None:
        self._embedder = embedder
        self._vector_store = vector_store

    async def link(self, query: str, *, top_k: int, min_score: float) -> list[Entity]:
        if not query.strip():
            return []
        [vector] = await self._embedder.embed_dense([query], is_query=True)
        matches = await self._vector_store.search_entities(vector, top_k=top_k, entity_type=None)
        return [entity for entity, score in matches if score >= min_score]


__all__ = ["EntityLinker"]
