"""State definitions for the orchestration layer."""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from graphrag.core.models import (
    Answer,
    BudgetLimits,
    GraphPath,
    NodeFailure,
    RoutePlan,
    ScoredChunk,
    Spend,
)


def merge_counters(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    """Key-wise addition over the union of keys. Associative and commutative."""
    result = dict(a)
    for k, v in b.items():
        result[k] = result.get(k, 0) + v
    return result


class QueryState(TypedDict):
    """LangGraph channel schema.

    Reducer rules:
        - `spent` accumulates CONSUMPTION. Never store 'remaining'.
        - Reducers receive (current, update) and nodes return only their DELTA.
    """

    correlation_id: str
    question: str  # IMMUTABLE. The user's original words. Never rewritten.
    active_query: str  # what retrieval actually uses; rewrite_query edits THIS
    plan: RoutePlan | None
    top_k: int | None  # from QueryRequest; None = use retrieval config default
    # POLICY ONLY, from `QueryRequest.strategy` — never a pre-seeded `RoutePlan` (BLUEPRINT
    # §6.4's override contract). The router does two separable jobs: choosing a strategy, which
    # a client can legitimately know, and extracting seed_entities/template/hops/relation_type/
    # sub_queries from the question, which a client cannot supply. Carrying the first as a whole
    # forged plan silently supplies the second as empty, and `seed_entities=[]` makes every graph
    # traversal return nothing. Single writer (the seed state), so no reducer.
    strategy_override: Literal["vector", "graph", "hybrid"] | None
    vector_hits: list[ScoredChunk]
    graph_hits: list[GraphPath]
    fused: list[ScoredChunk]
    graded: list[ScoredChunk]
    answer: Answer | None
    degraded: Annotated[list[str], operator.add]
    failures: Annotated[list[NodeFailure], operator.add]
    attempts: Annotated[dict[str, int], merge_counters]
    spent: Annotated[Spend, Spend.merge]


def remaining(state: QueryState, limits: BudgetLimits) -> BudgetLimits:
    """limits - state['spent']. Computed on read, never stored."""
    spent = state.get("spent", Spend())
    return BudgetLimits(
        max_llm_calls=max(0, limits.max_llm_calls - spent.llm_calls),
        max_wall_ms=max(0, limits.max_wall_ms - spent.wall_ms),
        max_prompt_tokens=max(0, limits.max_prompt_tokens - spent.tokens),
    )
