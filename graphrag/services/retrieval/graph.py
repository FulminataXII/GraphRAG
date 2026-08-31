"""`GraphRetriever` — entity-linked graph traversal, hydrated for the vector-outage fallback.
See BLUEPRINT §6.3.

Template selection (BLUEPRINT-patched `RoutePlan.template`/`RoutePlan.relation_type`):
    - `entities_by_relation` uses `plan.relation_type` directly — no entity linking involved.
    - `neighbors` and `top_entities_for_chunks` use `seed_entities[0]`, linked to its best-match
      canonical entity, as `entity_id`. (`top_entities_for_chunks` itself needs `chunk_ids`,
      which nothing in `RoutePlan` supplies, so it always falls back to `neighbors`.)
    - `path_between` uses `seed_entities[0]` and `seed_entities[1]` as `src_id`/`dst_id`.
    - `co_mentioned` uses `seed_entities[0]` as `chunk_id` — REPORTED MISMATCH: the Cypher
      template filters `RELATES.chunk_id = $chunk_id`, a chunk identifier, but the only value
      `RoutePlan` can supply here is a linked ENTITY's `canonical_id`. Wired exactly as
      directed; it will not raise, but in practice `co_mentioned` will always return zero rows
      through this path, since an entity id will not collide with a real chunk_id. Flagged for
      the BLUEPRINT to resolve (`RoutePlan` would need an actual chunk_id field), not silently
      "fixed" by inventing one here.
    - Whenever the requested template's parameters can't be built from `plan` (missing/short
      `seed_entities`, missing `relation_type`, or an unknown `template` string), retrieval
      falls back to `neighbors` with `seed_entities[0]`. If even that can't link, `retrieve()`
      returns `[]` — "Entity link miss -> empty, not error" (ARCHITECTURE).

Hydration: `GraphPath.chunks` (BLUEPRINT-patched) carries the hydrated `Chunk` objects directly,
so `fuse` (BO-10) can read them statelessly off each path — no side-channel attribute on this
class. `retrieve()` fetches the `Chunk`s for every surviving path's `chunk_ids` from
`graph_store` or `vector_store` per `retrieval.graph.hydrate_from`, then attaches the relevant
subset back onto each path via `model_copy` (`GraphPath` is frozen). This is also what
`test_graph_hydrates_from_neo4j` observes (no Qdrant call when `hydrate_from=neo4j`).

Constructor: scalars only (BLUEPRINT §0 — "a section model as a constructor parameter is fine"
for `services/`, but `get_settings()`/whole-`Settings` is not; see §5.2). No `Settings` object,
no `RetrievalSection`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import UUID

from graphrag.core.models import GraphPath, ScoredChunk

if TYPE_CHECKING:
    from graphrag.core.models import Chunk, RoutePlan
    from graphrag.core.ports import GraphStore, VectorStore
    from graphrag.services.retrieval.linker import EntityLinker

_KNOWN_TEMPLATES: Final[frozenset[str]] = frozenset(
    {"neighbors", "path_between", "entities_by_relation", "co_mentioned", "top_entities_for_chunks"}
)


class GraphRetriever:
    """Implements `retrieve()` (BLUEPRINT §6.3) — see module docstring for template selection
    and hydration.

    Contract of retrieve(plan, max_hops) -> list[GraphPath]:
        - Links plan.seed_entities; returns [] if none link.
        - Selects a template by plan.template and passes typed params including
          per_hop_cap = retrieval.graph.max_degree_per_hop.
        - Truncates to retrieval.graph.max_paths, ranked by path score.
        - Hydrates chunk text via retrieval.graph.hydrate_from ('neo4j' by default, which is
          what makes the vector-outage fallback real).
        - Attaches hydrated chunks directly to the returned GraphPath objects (chunks field).
    """

    def __init__(
        self,
        *,
        graph_store: GraphStore,
        vector_store: VectorStore,
        linker: EntityLinker,
        entity_link_top_k: int,
        entity_link_min_score: float,
        max_degree_per_hop: int,
        timeout_ms: int,
        max_paths: int,
        hydrate_from: Literal["neo4j", "qdrant"],
    ) -> None:
        self._graph_store = graph_store
        self._vector_store = vector_store
        self._linker = linker
        self._entity_link_top_k = entity_link_top_k
        self._entity_link_min_score = entity_link_min_score
        self._max_degree_per_hop = max_degree_per_hop
        self._timeout_ms = timeout_ms
        self._max_paths = max_paths
        self._hydrate_from = hydrate_from

    async def _link_one(self, seed_text: str) -> UUID | None:
        linked = await self._linker.link(
            seed_text, top_k=self._entity_link_top_k, min_score=self._entity_link_min_score
        )
        return linked[0].canonical_id if linked else None

    async def _params_for(self, template: str, plan: RoutePlan) -> dict[str, Any] | None:
        """Cypher params for `template` from `plan`, or None if `plan` doesn't carry enough --
        the caller falls back to 'neighbors' when this happens."""
        if template == "entities_by_relation":
            if plan.relation_type is None:
                return None
            return {"relation_type": plan.relation_type}

        if template == "top_entities_for_chunks":
            return None  # RoutePlan carries no chunk_ids -- always falls back to neighbors

        if not plan.seed_entities:
            return None
        first = await self._link_one(plan.seed_entities[0])
        if first is None:
            return None

        if template == "path_between":
            if len(plan.seed_entities) < 2:
                return None
            second = await self._link_one(plan.seed_entities[1])
            if second is None:
                return None
            return {"src_id": str(first), "dst_id": str(second)}

        if template == "co_mentioned":
            # See module docstring's REPORTED MISMATCH -- chunk_id here is really an entity id.
            return {"chunk_id": str(first)}

        # "neighbors" (and the universal fallback target).
        return {"entity_id": str(first)}

    async def retrieve(self, plan: RoutePlan, max_hops: int) -> list[GraphPath]:
        template = plan.template if plan.template in _KNOWN_TEMPLATES else "neighbors"
        params = await self._params_for(template, plan)
        if params is None:
            if template == "neighbors":
                return []  # neighbors itself couldn't link -- nothing left to fall back to
            template = "neighbors"
            params = await self._params_for(template, plan)
            if params is None:
                return []

        found = await self._graph_store.traverse(
            template,
            {**params, "per_hop_cap": self._max_degree_per_hop},
            timeout_ms=self._timeout_ms,
        )

        effective_max_hops = min(plan.hops, max_hops)
        paths = [path for path in found if path.hops <= effective_max_hops]
        paths.sort(key=lambda p: p.score, reverse=True)
        paths = paths[: self._max_paths]

        chunk_ids = sorted({cid for path in paths for cid in path.chunk_ids}, key=str)
        hydrated_by_id: dict[UUID, Chunk] = {}
        if chunk_ids:
            source = self._graph_store if self._hydrate_from == "neo4j" else self._vector_store
            hydrated = await source.get_chunks(chunk_ids)
            hydrated_by_id = {chunk.chunk_id: chunk for chunk in hydrated}

        return [
            path.model_copy(
                update={
                    "chunks": [
                        hydrated_by_id[cid] for cid in path.chunk_ids if cid in hydrated_by_id
                    ]
                }
            )
            for path in paths
        ]


def graph_paths_to_scored_chunks(paths: Sequence[GraphPath]) -> list[ScoredChunk]:
    """One `ScoredChunk` (origin='graph') per unique chunk touched by `paths[*].chunks`, in
    path-score order — the bridge `fusion.reciprocal_rank_fusion` needs (it consumes
    `list[ScoredChunk]`, not `list[GraphPath]`) but that BLUEPRINT §6.3 contract doesn't itself
    build."""
    best_score: dict[UUID, float] = {}
    chunk_by_id: dict[UUID, Chunk] = {}
    for path in paths:
        for chunk in path.chunks:
            if chunk.chunk_id not in best_score or path.score > best_score[chunk.chunk_id]:
                best_score[chunk.chunk_id] = path.score
                chunk_by_id[chunk.chunk_id] = chunk

    ordered = sorted(best_score, key=lambda cid: (-best_score[cid], str(cid)))
    return [
        ScoredChunk(
            chunk=chunk_by_id[chunk_id], score=best_score[chunk_id], rank=rank, origin="graph"
        )
        for rank, chunk_id in enumerate(ordered, start=1)
    ]


__all__ = ["GraphRetriever", "graph_paths_to_scored_chunks"]
