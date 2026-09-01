from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from graphrag.services.orchestration.graph import NodeDeps
from graphrag.services.orchestration.state import QueryState


async def node(state: QueryState, deps: NodeDeps) -> dict[str, Any]:
    # Bookkeeping node, does nothing but increment attempts.
    # The actual conditional routing logic decides what to retry based on failures.
    return {"attempts": {"repair": 1}}
