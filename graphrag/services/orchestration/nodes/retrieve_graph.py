from __future__ import annotations

from typing import TYPE_CHECKING, Any

from graphrag.core.errors import GraphBackendUnavailable
from graphrag.core.models import NodeFailure

if TYPE_CHECKING:
    from graphrag.services.orchestration.graph import NodeDeps
from graphrag.services.orchestration.state import QueryState


async def node(state: QueryState, deps: NodeDeps) -> dict[str, Any]:
    plan = state.get("plan")
    # See `retrieve_vector.node` -- this node also re-runs on the rewrite loop.
    attempt = state.get("attempts", {}).get("retrieve_graph", 0) + 1
    counted: dict[str, Any] = {"attempts": {"retrieve_graph": 1}}
    if not plan:
        return {**counted, "graph_hits": []}

    max_hops = deps.settings.retrieval.graph.max_hops
    try:
        graph_hits = await deps.graph.retrieve(plan, max_hops)
        return {**counted, "graph_hits": graph_hits}
    except GraphBackendUnavailable as e:
        return {
            **counted,
            "graph_hits": [],
            "degraded": ["graph"],
            "failures": [
                NodeFailure(
                    node="retrieve_graph",
                    code=e.code,
                    message=str(e),
                    attempt=attempt,
                    at=deps.clock.now(),
                )
            ],
        }
