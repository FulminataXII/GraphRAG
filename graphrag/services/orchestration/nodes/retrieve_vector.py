from __future__ import annotations

from typing import TYPE_CHECKING, Any

from graphrag.core.errors import RetrievalBackendUnavailable
from graphrag.core.models import NodeFailure

if TYPE_CHECKING:
    from graphrag.services.orchestration.graph import NodeDeps
from graphrag.services.orchestration.state import QueryState


async def node(state: QueryState, deps: NodeDeps) -> dict[str, Any]:
    active_query = state.get("active_query", "")
    top_k = deps.settings.retrieval.vector.top_k

    try:
        vector_hits = await deps.vector.retrieve(active_query, top_k)
        return {"vector_hits": vector_hits}
    except RetrievalBackendUnavailable as e:
        return {
            "vector_hits": [],
            "degraded": ["vector"],
            "failures": [
                NodeFailure(
                    node="retrieve_vector",
                    code=e.code,
                    message=str(e),
                    attempt=1,
                    at=deps.clock.now(),
                )
            ],
        }
