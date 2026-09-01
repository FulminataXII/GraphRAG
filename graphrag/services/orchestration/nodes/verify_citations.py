from __future__ import annotations

from typing import TYPE_CHECKING, Any

from graphrag.core.models import NodeFailure

if TYPE_CHECKING:
    from graphrag.services.orchestration.graph import NodeDeps
from graphrag.services.orchestration.state import QueryState


async def node(state: QueryState, deps: NodeDeps) -> dict[str, Any]:
    answer = state.get("answer")
    graded = state.get("graded", [])
    if not answer:
        return {}

    valid_ids = {str(c.chunk.chunk_id) for c in graded}
    invalid_ids = []

    for citation in answer.citations:
        if str(citation.chunk_id) not in valid_ids:
            invalid_ids.append(str(citation.chunk_id))

    if invalid_ids:
        deps.metrics.citations_invalid.add(1)
        return {
            "failures": [
                NodeFailure(
                    node="verify_citations",
                    code="CITATION_INVALID",
                    message=f"Answer cites chunk_ids not in context: {', '.join(invalid_ids)}",
                    attempt=1,
                    at=deps.clock.now(),
                )
            ]
        }

    return {}
