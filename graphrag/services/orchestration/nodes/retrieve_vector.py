from __future__ import annotations

from typing import TYPE_CHECKING, Any

from graphrag.core.errors import RetrievalBackendUnavailable
from graphrag.core.models import NodeFailure

if TYPE_CHECKING:
    from graphrag.services.orchestration.graph import NodeDeps
from graphrag.services.orchestration.state import QueryState


async def node(state: QueryState, deps: NodeDeps) -> dict[str, Any]:
    active_query = state.get("active_query", "")
    top_k = state.get("top_k") or deps.settings.retrieval.vector.top_k
    # `rewrite_query` loops back through retrieval, so this node runs more than once per query.
    # See `verify_grounded.node` for why the count tracks executions rather than failures.
    attempt = state.get("attempts", {}).get("retrieve_vector", 0) + 1
    counted: dict[str, Any] = {"attempts": {"retrieve_vector": 1}}

    try:
        vector_hits = await deps.vector.retrieve(active_query, top_k)
        return {**counted, "vector_hits": vector_hits}
    except RetrievalBackendUnavailable as e:
        return {
            **counted,
            "vector_hits": [],
            "degraded": ["vector"],
            "failures": [
                NodeFailure(
                    node="retrieve_vector",
                    code=e.code,
                    message=str(e),
                    attempt=attempt,
                    at=deps.clock.now(),
                )
            ],
        }
