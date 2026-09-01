from __future__ import annotations

from typing import TYPE_CHECKING, Any

from graphrag.core.errors import ValidationError

if TYPE_CHECKING:
    from graphrag.services.orchestration.graph import NodeDeps
from graphrag.services.orchestration.state import QueryState


async def node(state: QueryState, deps: NodeDeps) -> dict[str, Any]:
    question = state.get("question", "").strip()
    if not question:
        raise ValidationError("Query cannot be empty.")
    if len(question) > deps.settings.limits.max_query_chars:
        raise ValidationError(
            f"Query exceeds max length of {deps.settings.limits.max_query_chars} characters."
        )

    return {"active_query": question}
