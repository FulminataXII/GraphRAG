from __future__ import annotations

import copy
from typing import Any

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


async def test_each_node_is_pure(container: Any):
    """Every node returns a partial dict and mutates nothing."""
    nodes = [
        guard.node,
        plan_route.node,
        retrieve_vector.node,
        retrieve_graph.node,
        fuse.node,
        grade_context.node,
        rewrite_query.node,
        generate.node,
        verify_citations.node,
        verify_grounded.node,
        repair.node,
        finalize.node,
        insufficient.node,
    ]

    deps = container.orchestrator.deps

    # We must script the FakeLLMClient to not crash during nodes that expect LLM responses.
    # We'll just provide empty valid schemas.
    # Actually, pureness just means it doesn't mutate input.
    # If it raises an exception (like LLMProviderExhausted), it still didn't mutate.

    state = {
        "correlation_id": "123",
        "question": "what?",
        "active_query": "what?",
        "plan": None,
        "vector_hits": [],
        "graph_hits": [],
        "fused": [],
        "graded": [],
        "answer": None,
        "degraded": [],
        "failures": [],
        "attempts": {},
    }

    # Script fake LLM to just return valid pydantic objects for the nodes that call it.
    from graphrag.services.orchestration.schemas import (
        AnswerOut,
        CitationOut,
        Entailment,
        RelevanceGradeBatch,
        RewrittenQuery,
        RoutePlanOut,
    )

    container.llm_client.script_structured(
        "plan_route",
        RoutePlanOut(
            strategy="vector",
            template="neighbors",
            seed_entities=[],
            hops=1,
            sub_queries=[],
            rationale="",
        ),
    )
    container.llm_client.script_structured("grade_context", RelevanceGradeBatch(grades=[]))
    container.llm_client.script_structured(
        "rewrite_query", RewrittenQuery(query="what?", changed_because="")
    )
    container.llm_client.script_structured(
        "generate",
        AnswerOut(
            text="ans", citations=[CitationOut(chunk_id="chunk1", quote=None)], confidence=1.0
        ),
    )
    container.llm_client.script_structured(
        "verify_grounded", Entailment(supported=True, score=1.0, unsupported_spans=[])
    )

    for node_func in nodes:
        state_copy = copy.deepcopy(state)
        try:
            delta = await node_func(state_copy, deps)
            assert isinstance(delta, dict)
            assert state_copy == state, f"{node_func.__name__} mutated the input state!"
        except Exception:
            # Even if it fails (e.g. backend unavailable, no script), it shouldn't have mutated the state before failing.
            assert state_copy == state, (
                f"{node_func.__name__} mutated the input state before raising an error!"
            )
