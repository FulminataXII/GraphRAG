from __future__ import annotations

from typing import TYPE_CHECKING, Any

from graphrag.core.models import Spend

if TYPE_CHECKING:
    from graphrag.services.orchestration.graph import NodeDeps
from graphrag.services.orchestration.prompts import render
from graphrag.services.orchestration.schemas import RewrittenQuery
from graphrag.services.orchestration.state import QueryState


async def node(state: QueryState, deps: NodeDeps) -> dict[str, Any]:
    question = state["question"]
    # We pass the active_query to let the LLM know what was tried, maybe?
    # Actually, BLUEPRINT doesn't explicitly say what's in rewrite_query.j2 except 'question' or similar.
    active_query = state.get("active_query", question)

    prompt = render("rewrite_query.j2", question=question, active_query=active_query)

    result = await deps.llm.structured(
        role="router",
        messages=[{"role": "user", "content": prompt}],
        schema=RewrittenQuery,
        max_repairs=deps.settings.orchestration.max_structured_output_repairs,
    )
    spent = Spend(
        llm_calls=result.repair_attempts + 1,
        tokens=result.prompt_tokens + result.completion_tokens,
        wall_ms=result.latency_ms,
    )

    return {
        "active_query": result.value.query,
        "spent": spent,
        "attempts": {"rewrite_query": 1},
    }
