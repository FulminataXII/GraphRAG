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

    # Scripted by LLM ROLE, which is what `deps.llm.structured(role=...)` is called with — not
    # by node name. These five queues used to be keyed "plan_route"/"grade_context"/
    # "rewrite_query"/"generate"/"verify_grounded", none of which any node ever requests, so
    # every LLM-backed node hit an empty queue, raised `LLMProviderExhausted`, and landed in the
    # `except` branch below. The success path — the one that checks a node returns a dict — has
    # never run for any of them.
    #
    # `router` serves BOTH plan_route and rewrite_query, so its queue carries one of each shape;
    # `FakeLLMClient` dispenses the first item matching the requested schema.
    container.llm_client.script_structured(
        "router",
        RoutePlanOut(
            strategy="vector",
            template="neighbors",
            seed_entities=[],
            hops=1,
            sub_queries=[],
            rationale="",
        ),
        RewrittenQuery(query="what?", changed_because=""),
    )
    container.llm_client.script_structured("grader", RelevanceGradeBatch(grades=[]))
    container.llm_client.script_structured(
        "synth",
        AnswerOut(
            text="ans", citations=[CitationOut(chunk_id="chunk1", quote=None)], confidence=1.0
        ),
    )
    container.llm_client.script_structured(
        "judge", Entailment(supported=True, score=1.0, unsupported_spans=[])
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


async def test_grader_schema_violation_is_reported_not_just_swallowed(container: Any):
    """A grader that cannot grade must SAY so.

    `orchestration.grader.fail_open: true` is a defensible choice — a broken grader should not
    take the query down. Failing open *silently* is not: every chunk is passed through ungraded,
    `orchestration.min_relevant_docs` is then trivially satisfied, `route_after_grade` never
    routes to `rewrite_query`, and the response looks identical to a query whose context really
    was relevant. The self-correction loop has a limb that cannot move and nothing says so.

    So the pass-through stays, and a `NodeFailure` plus a `degraded` entry are recorded alongside
    it — the same contract `retrieve_vector`/`retrieve_graph` already honour for their backends.
    """
    from graphrag.core.errors import LLMSchemaViolation
    from graphrag.core.models import ScoredChunk
    from tests.factories import make_chunk

    deps = container.orchestrator.deps
    container.llm_client.script_structured("grader", LLMSchemaViolation("unparseable"))

    fused = [
        ScoredChunk(chunk=make_chunk(f"chunk {i}"), score=1.0 - i / 10, rank=i, origin="vector")
        for i in range(1, 4)
    ]
    state = {
        "correlation_id": "cid",
        "question": "what?",
        "active_query": "what?",
        "fused": fused,
        "graded": [],
        "degraded": [],
        "failures": [],
        "attempts": {},
    }

    update = await grade_context.node(state, deps)

    # Fail-open behaviour is unchanged: nothing is dropped.
    assert [c.chunk.chunk_id for c in update["graded"]] == [c.chunk.chunk_id for c in fused]

    # ...but it is now visible.
    assert update["degraded"] == ["grader"]
    assert [f.node for f in update["failures"]] == ["grade_context"]
    assert update["failures"][0].code == LLMSchemaViolation.code
    assert update["failures"][0].attempt == 1


async def test_grader_degraded_is_recorded_once_across_batches(container: Any):
    """`degraded` is `Annotated[list[str], operator.add]`, so a per-batch append would write
    "grader" once per batch and make a two-batch query look twice as broken as a one-batch one."""
    from graphrag.core.errors import LLMSchemaViolation
    from graphrag.core.models import ScoredChunk
    from tests.factories import make_chunk

    deps = container.orchestrator.deps
    batch_size = deps.settings.orchestration.grader.batch_size
    for _ in range(3):
        container.llm_client.script_structured("grader", LLMSchemaViolation("unparseable"))

    fused = [
        ScoredChunk(chunk=make_chunk(f"chunk {i}"), score=1.0, rank=i, origin="vector")
        for i in range(batch_size + 2)
    ]
    state = {
        "correlation_id": "cid",
        "question": "what?",
        "active_query": "what?",
        "fused": fused,
        "graded": [],
        "degraded": [],
        "failures": [],
        "attempts": {},
    }

    update = await grade_context.node(state, deps)

    assert update["degraded"] == ["grader"]
    assert len(update["failures"]) == 2  # one per batch, so the count is still legible


async def test_grader_partial_response_is_a_failure_not_a_partial_success(container: Any):
    """A grade count that doesn't match the chunk count is a FAILURE, not a partial success.

    `grader.max_tokens` bounds the whole completion and `fast-low-latency` is a reasoning model,
    so a batch can come back as syntactically valid JSON carrying grades for only the first few
    chunks (measured at max_tokens 600: finish_reason=length, 2 grades for 8 chunks). Nothing
    raises, so the fail-open path never runs, and the unmentioned chunks are dropped as "not
    relevant" purely because the model ran out of room to mention them.

    The asymmetry decides which way to fail: an extra irrelevant chunk is recoverable downstream
    -- `verify_grounded` and the citation checks both operate on what `generate` actually used --
    while a silently dropped chunk is recoverable by nothing. It is simply absent, and the answer
    is worse with no signal that it happened. So the whole batch survives and the run is marked
    degraded.
    """
    from graphrag.core.models import ScoredChunk
    from graphrag.services.orchestration.schemas import RelevanceGrade, RelevanceGradeBatch
    from tests.factories import make_chunk

    deps = container.orchestrator.deps
    fused = [
        ScoredChunk(chunk=make_chunk(f"chunk {i}"), score=1.0 - i / 10, rank=i, origin="vector")
        for i in range(1, 4)
    ]
    # Grades for the first chunk only: the other two are never mentioned.
    container.llm_client.script_structured(
        "grader",
        RelevanceGradeBatch(
            grades=[
                RelevanceGrade(
                    chunk_id=str(fused[0].chunk.chunk_id), relevant=True, reason="on topic"
                )
            ]
        ),
    )

    state = {
        "correlation_id": "cid",
        "question": "what?",
        "active_query": "what?",
        "fused": fused,
        "graded": [],
        "degraded": [],
        "failures": [],
        "attempts": {},
    }

    update = await grade_context.node(state, deps)

    # Every chunk survives, including the two the grader never reached.
    assert [c.chunk.chunk_id for c in update["graded"]] == [c.chunk.chunk_id for c in fused]
    assert update["degraded"] == ["grader"]
    assert len(update["failures"]) == 1
    assert update["failures"][0].node == "grade_context"
    assert update["failures"][0].code == "GRADER_INCOMPLETE"


async def test_grader_full_response_is_not_degraded(container: Any):
    """The counterpart: a grade per chunk is a normal run, however it grades them. Without this,
    the count check above could be satisfied by marking every run degraded."""
    from graphrag.core.models import ScoredChunk
    from graphrag.services.orchestration.schemas import RelevanceGrade, RelevanceGradeBatch
    from tests.factories import make_chunk

    deps = container.orchestrator.deps
    fused = [
        ScoredChunk(chunk=make_chunk(f"chunk {i}"), score=1.0, rank=i, origin="vector")
        for i in range(1, 4)
    ]
    container.llm_client.script_structured(
        "grader",
        RelevanceGradeBatch(
            grades=[
                RelevanceGrade(chunk_id=str(c.chunk.chunk_id), relevant=(i == 0), reason="graded")
                for i, c in enumerate(fused)
            ]
        ),
    )

    state = {
        "correlation_id": "cid",
        "question": "what?",
        "active_query": "what?",
        "fused": fused,
        "graded": [],
        "degraded": [],
        "failures": [],
        "attempts": {},
    }

    update = await grade_context.node(state, deps)

    # Graded, and the irrelevant ones really are dropped -- the count check must not turn the
    # grader into a pass-through.
    assert [c.chunk.chunk_id for c in update["graded"]] == [fused[0].chunk.chunk_id]
    assert "degraded" not in update
    assert "failures" not in update
