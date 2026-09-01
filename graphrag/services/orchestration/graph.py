from __future__ import annotations

from collections.abc import AsyncIterator
from functools import partial
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from graphrag.config.settings import Settings
from graphrag.core.models import Answer, RoutePlan, Spend
from graphrag.core.ports import Clock, LLMClient
from graphrag.services.orchestration.nodes import (
    finalize,
    fuse,
    generate,
    grade_context,
    guard,
    insufficient,
    plan_route,
    repair,
    retrieve_graph,
    retrieve_vector,
    rewrite_query,
    verify_citations,
    verify_grounded,
)
from graphrag.services.orchestration.state import QueryState, remaining
from graphrag.services.retrieval.graph import GraphRetriever
from graphrag.services.retrieval.linker import EntityLinker
from graphrag.services.retrieval.vector import VectorRetriever


class NodeDeps(BaseModel):
    """Everything nodes need. Passed to every node; nodes never reach for globals."""

    model_config = {"arbitrary_types_allowed": True}

    llm: LLMClient
    vector: VectorRetriever
    graph: GraphRetriever
    linker: EntityLinker
    metrics: Any
    settings: Settings
    clock: Clock


class QueryResult(BaseModel):
    answer: Answer
    route: RoutePlan | None
    degraded: list[str]
    spent: Spend
    correlation_id: str


class StreamEvent(BaseModel):
    event: Literal["node_start", "node_end", "token", "done", "error"]
    node: str | None = None
    data: Any = None


def route_after_guard(state: QueryState, deps: NodeDeps) -> str:
    limits = deps.settings.limits.per_request_budget
    rem = remaining(state, limits)
    if rem.max_llm_calls <= 0 or rem.max_prompt_tokens <= 0 or rem.max_wall_ms <= 0:
        return "insufficient"
    return "plan_route"


def route_after_plan(state: QueryState, deps: NodeDeps) -> list[str]:
    plan = state.get("plan")
    strategy = plan.strategy if plan else deps.settings.orchestration.default_strategy
    if strategy == "vector":
        return ["retrieve_vector"]
    elif strategy == "graph":
        return ["retrieve_graph"]
    else:
        return ["retrieve_vector", "retrieve_graph"]


def route_after_grade(state: QueryState, deps: NodeDeps) -> str:
    limits = deps.settings.limits.per_request_budget
    rem = remaining(state, limits)
    if rem.max_llm_calls <= 0 or rem.max_prompt_tokens <= 0 or rem.max_wall_ms <= 0:
        return "insufficient"

    graded = state.get("graded", [])
    if len(graded) >= deps.settings.orchestration.min_relevant_docs:
        return "generate"

    attempts = state.get("attempts", {})
    if attempts.get("rewrite_query", 0) >= deps.settings.orchestration.max_query_rewrites:
        return "insufficient"

    return "rewrite_query"


def route_after_citations(state: QueryState, deps: NodeDeps) -> str:
    failures = state.get("failures", [])
    if failures and failures[-1].node == "verify_citations":
        return "repair"

    if not deps.settings.orchestration.verification.check_groundedness:
        return "finalize"

    limits = deps.settings.limits.per_request_budget
    rem = remaining(state, limits)
    if rem.max_llm_calls <= 0 or rem.max_prompt_tokens <= 0 or rem.max_wall_ms <= 0:
        return "insufficient"

    return "verify_grounded"


def route_after_grounded(state: QueryState, deps: NodeDeps) -> str:
    failures = state.get("failures", [])
    if failures and failures[-1].node == "verify_grounded":
        return "repair"
    return "finalize"


def route_after_repair(state: QueryState, deps: NodeDeps) -> str:
    attempts = state.get("attempts", {})
    if attempts.get("repair", 0) >= deps.settings.orchestration.max_repair_attempts:
        return "insufficient"

    limits = deps.settings.limits.per_request_budget
    rem = remaining(state, limits)
    if rem.max_llm_calls <= 0 or rem.max_prompt_tokens <= 0 or rem.max_wall_ms <= 0:
        return "insufficient"

    return "generate"


def build_query_graph(deps: NodeDeps, settings: Settings) -> CompiledStateGraph:  # type: ignore
    workflow = StateGraph(QueryState)

    # Add nodes
    workflow.add_node("guard", partial(guard.node, deps=deps))
    workflow.add_node("plan_route", partial(plan_route.node, deps=deps))
    workflow.add_node("retrieve_vector", partial(retrieve_vector.node, deps=deps))
    workflow.add_node("retrieve_graph", partial(retrieve_graph.node, deps=deps))
    workflow.add_node("fuse", partial(fuse.node, deps=deps))
    workflow.add_node("grade_context", partial(grade_context.node, deps=deps))
    workflow.add_node("rewrite_query", partial(rewrite_query.node, deps=deps))
    workflow.add_node("generate", partial(generate.node, deps=deps))
    workflow.add_node("verify_citations", partial(verify_citations.node, deps=deps))
    workflow.add_node("verify_grounded", partial(verify_grounded.node, deps=deps))
    workflow.add_node("repair", partial(repair.node, deps=deps))
    workflow.add_node("finalize", partial(finalize.node, deps=deps))
    workflow.add_node("insufficient", partial(insufficient.node, deps=deps))

    # Add edges
    workflow.add_edge(START, "guard")
    workflow.add_conditional_edges("guard", partial(route_after_guard, deps=deps))

    workflow.add_conditional_edges(
        "plan_route",
        partial(route_after_plan, deps=deps),
        ["retrieve_vector", "retrieve_graph"],
    )

    workflow.add_edge("retrieve_vector", "fuse")
    workflow.add_edge("retrieve_graph", "fuse")

    workflow.add_edge("fuse", "grade_context")

    workflow.add_conditional_edges("grade_context", partial(route_after_grade, deps=deps))

    workflow.add_edge("rewrite_query", "plan_route")

    workflow.add_edge("generate", "verify_citations")

    workflow.add_conditional_edges("verify_citations", partial(route_after_citations, deps=deps))

    workflow.add_conditional_edges("verify_grounded", partial(route_after_grounded, deps=deps))

    workflow.add_conditional_edges("repair", partial(route_after_repair, deps=deps))

    workflow.add_edge("finalize", END)
    workflow.add_edge("insufficient", END)

    return workflow.compile()


class OrchestrationService:
    def __init__(self, deps: NodeDeps) -> None:
        self.deps = deps
        self.graph = build_query_graph(deps, deps.settings)

    async def run(self, question: str, correlation_id: str) -> QueryResult:
        state: dict[str, Any] = {
            "correlation_id": correlation_id,
            "question": question,
            "active_query": question,
            "plan": None,
            "vector_hits": [],
            "graph_hits": [],
            "fused": [],
            "graded": [],
            "answer": None,
            "degraded": [],
            "failures": [],
            "attempts": {},
            "spent": Spend(),
        }
        final_state = await self.graph.ainvoke(state)
        return QueryResult(
            answer=final_state["answer"],
            route=final_state.get("plan"),
            degraded=list(set(final_state.get("degraded", []))),
            spent=final_state.get("spent", Spend()),
            correlation_id=final_state["correlation_id"],
        )

    async def stream(self, question: str, correlation_id: str) -> AsyncIterator[StreamEvent]:
        # Trivial stream implementation since real SSE involves LangGraph ASTREAM which might be complex
        # For BO-10 we just need basic node events emitted in order.
        state: dict[str, Any] = {
            "correlation_id": correlation_id,
            "question": question,
            "active_query": question,
            "plan": None,
            "vector_hits": [],
            "graph_hits": [],
            "fused": [],
            "graded": [],
            "answer": None,
            "degraded": [],
            "failures": [],
            "attempts": {},
            "spent": Spend(),
        }

        async for event in self.graph.astream_events(state, version="v1"):
            kind = event["event"]
            node = event.get("name")

            if kind == "on_chain_start" and event.get("type") == "chain":
                if self.deps.settings.orchestration.streaming.emit_node_events and node:
                    yield StreamEvent(event="node_start", node=node)
            elif kind == "on_chain_end" and event.get("type") == "chain":
                if self.deps.settings.orchestration.streaming.emit_node_events and node:
                    yield StreamEvent(event="node_end", node=node)
            elif kind == "on_chat_model_stream":
                # Assuming LiteLLM yields chunks that langgraph captures
                chunk = event["data"]["chunk"]
                if hasattr(chunk, "content") and chunk.content:
                    yield StreamEvent(event="token", data=chunk.content)

        yield StreamEvent(event="done")
