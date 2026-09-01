from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from graphrag.core.models import ScoredChunk

if TYPE_CHECKING:
    from graphrag.services.orchestration.graph import NodeDeps
from graphrag.services.orchestration.state import QueryState
from graphrag.services.retrieval.fusion import reciprocal_rank_fusion


async def node(state: QueryState, deps: NodeDeps) -> dict[str, Any]:
    vector_hits = state.get("vector_hits", [])
    graph_hits = state.get("graph_hits", [])

    if not vector_hits and not graph_hits:
        return {"fused": []}

    graph_chunks_map: dict[UUID, ScoredChunk] = {}
    for path in graph_hits:
        for chunk in path.chunks:
            if (
                chunk.chunk_id not in graph_chunks_map
                or path.score > graph_chunks_map[chunk.chunk_id].score
            ):
                graph_chunks_map[chunk.chunk_id] = ScoredChunk(
                    chunk=chunk,
                    score=path.score,
                    rank=0,
                    origin="graph",
                )

    sorted_graph = sorted(graph_chunks_map.values(), key=lambda c: c.score, reverse=True)
    for i, c in enumerate(sorted_graph, start=1):
        # We need a new ScoredChunk to update the rank since pydantic models are frozen
        sorted_graph[i - 1] = c.model_copy(update={"rank": i})

    fusion_settings = deps.settings.retrieval.fusion
    weight_vector = fusion_settings.weights.get("vector", 1.0)
    weight_graph = fusion_settings.weights.get("graph", 1.0)

    fused = reciprocal_rank_fusion(
        [vector_hits, sorted_graph],
        k=fusion_settings.rrf_k,
        weights=[weight_vector, weight_graph],
        top_n=fusion_settings.final_top_k,
    )
    return {"fused": fused}
