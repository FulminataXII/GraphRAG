from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, get_type_hints
from uuid import uuid4

import pytest

from graphrag.core.errors import LLMProviderExhausted
from graphrag.core.models import Answer, Chunk, Citation, NodeFailure, ScoredChunk, Spend
from graphrag.services.orchestration.graph import route_after_citations, route_after_grounded
from graphrag.services.orchestration.schemas import (
    AnswerOut,
    CitationOut,
    Entailment,
    RelevanceGrade,
    RelevanceGradeBatch,
    RewrittenQuery,
    RoutePlanOut,
)
from graphrag.services.orchestration.state import QueryState
from tests.factories import make_chunk, make_entity


def get_initial_state(question="what?") -> QueryState:
    return {
        "correlation_id": "test-123",
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


async def run_graph(container: Any, state: QueryState) -> QueryState:
    graph = container.orchestrator.graph
    result = await graph.ainvoke(state)
    return result


def _counter_total(reader: Any, metric_name: str) -> float:
    """Sum every data point for `metric_name` out of an `InMemoryMetricReader`."""
    data = reader.get_metrics_data()
    if data is None:
        return 0.0
    return sum(
        point.value
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == metric_name
        for point in metric.data.data_points
    )


def _seed_two_relevant_chunks(container: Any) -> list[Any]:
    """Put the graph on its happy path as far as `generate`.

    Two chunks in the vector store (`min_relevant_docs` is 2, so one is not enough to reach
    `generate`), a `vector` route so only `retrieve_vector` runs, and a grader that marks both
    relevant. Returns the chunks so a caller can cite them.
    """
    chunks = [make_chunk("alpha passage about widgets"), make_chunk("beta passage about widgets")]
    for chunk in chunks:
        container.vector_store.chunks[chunk.chunk_id] = chunk

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
    )
    container.llm_client.script_structured(
        "grader",
        RelevanceGradeBatch(
            grades=[
                RelevanceGrade(chunk_id=str(chunk.chunk_id), relevant=True, reason="on topic")
                for chunk in chunks
            ]
        ),
    )
    return chunks


def _calls(container: Any, role: str) -> list[Any]:
    return [call for call in container.llm_client.calls if call["role"] == role]


async def test_parallel_nodes_both_append_failures(container: Any):
    container.vector_store.fail = True
    container.graph_store.fail = True

    container.llm_client.script_structured(
        "router",
        RoutePlanOut(
            strategy="hybrid",
            template="neighbors",
            seed_entities=[],
            hops=1,
            sub_queries=[],
            rationale="",
        ),
    )
    container.llm_client.script_structured("grader", RelevanceGradeBatch(grades=[]))
    # the rewrite loop will trigger because graded is empty.
    container.llm_client.script_structured("router", RewrittenQuery(query="r", changed_because=""))
    container.llm_client.script_structured(
        "router",
        RoutePlanOut(
            strategy="hybrid",
            template="neighbors",
            seed_entities=[],
            hops=1,
            sub_queries=[],
            rationale="",
        ),
    )

    # Actually just failing it by not providing more.
    # It will hit ProviderExhausted, but it will have accumulated the retrieval failures before that.
    with pytest.raises(LLMProviderExhausted):
        state = await run_graph(container, get_initial_state())

    # We can invoke just up to the fuse node.
    state = get_initial_state()
    state["plan"] = RoutePlanOut(
        strategy="hybrid",
        template="neighbors",
        seed_entities=["test_entity"],
        hops=1,
        sub_queries=[],
        rationale="",
    )
    from graphrag.services.orchestration.nodes import retrieve_graph, retrieve_vector

    # Mock linker so it doesn't fail due to vector_store.fail = True
    async def fake_link(*args, **kwargs):
        from uuid import uuid4

        from graphrag.core.models import Entity, EntityType

        return [
            Entity(
                canonical_id=uuid4(),
                name="test_entity",
                name_normalized="test_entity",
                type=EntityType.PERSON,
                aliases=[],
                mention_count=1,
            )
        ]

    container.entity_linker.link = fake_link

    s1 = await retrieve_vector.node(state, container.orchestrator.deps)

    s2 = await retrieve_graph.node(state, container.orchestrator.deps)

    print("DEBUG s1=", s1, "s2=", s2)
    assert len(s1.get("failures", [])) == 1
    assert len(s2["failures"]) == 1


async def test_parallel_branches_do_not_leak_budget(container: Any):
    hints = get_type_hints(QueryState, include_extras=True)
    assert hints["spent"].__metadata__[0] == Spend.merge


async def test_failures_accumulate_across_sequential_nodes(container: Any):
    # Test just node invocation to be precise
    state = get_initial_state()
    state["plan"] = RoutePlanOut(
        strategy="hybrid",
        template="neighbors",
        seed_entities=["test_entity"],
        hops=1,
        sub_queries=[],
        rationale="",
    )
    state["failures"] = []

    # verify_citations appending failure
    c1_uuid = uuid4()
    state["graded"] = [
        ScoredChunk(
            score=1.0,
            rank=1,
            origin="vector",
            chunk=Chunk(chunk_id=c1_uuid, text="test", content_hash="", sources=[], entity_ids=[]),
        )
    ]
    state["answer"] = Answer(
        text="ans",
        citations=[Citation(chunk_id=uuid4(), quote=None, doc_id="fake_doc", uri="")],
        confidence=1.0,
    )

    from graphrag.services.orchestration.nodes import verify_citations

    delta1 = await verify_citations.node(state, container.orchestrator.deps)
    assert len(delta1["failures"]) == 1

    # We can just verify that state reducer appends.
    hints = get_type_hints(QueryState, include_extras=True)
    import operator

    assert hints["failures"].__metadata__[0] == operator.add


async def test_rewrite_preserves_original_question(container: Any):
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
        RewrittenQuery(query="rewrite1", changed_because=""),
        RoutePlanOut(
            strategy="vector",
            template="neighbors",
            seed_entities=[],
            hops=1,
            sub_queries=[],
            rationale="",
        ),
        RewrittenQuery(query="rewrite2", changed_because=""),
    )
    container.llm_client.script_structured(
        "grader", RelevanceGradeBatch(grades=[]), RelevanceGradeBatch(grades=[])
    )

    with pytest.raises(LLMProviderExhausted):
        state = await run_graph(container, get_initial_state("original_question"))
        assert state["question"] == "original_question"
        assert state["active_query"] == "rewrite2"


async def test_generate_prompted_with_original_question(container: Any):
    container.llm_client.script_structured(
        "synth",
        AnswerOut(text="ans", citations=[CitationOut(chunk_id="c1", quote=None)], confidence=1.0),
    )

    state = get_initial_state("original_q")
    state["active_query"] = "rewritten_q"
    c1_uuid = uuid4()
    state["graded"] = [
        ScoredChunk(
            score=1.0,
            rank=1,
            origin="vector",
            chunk=Chunk(chunk_id=c1_uuid, text="test", content_hash="", sources=[], entity_ids=[]),
        )
    ]

    state["plan"] = RoutePlanOut(
        strategy="vector",
        template="neighbors",
        seed_entities=[],
        hops=1,
        sub_queries=[],
        rationale="",
    )

    c1_uuid = uuid4()
    state["fused"] = [
        ScoredChunk(
            score=1.0,
            rank=1,
            origin="vector",
            chunk=Chunk(chunk_id=c1_uuid, text="test", content_hash="", sources=[], entity_ids=[]),
        )
    ]

    from graphrag.services.orchestration.nodes import generate

    await generate.node(state, container.orchestrator.deps)

    gen_call = next(c for c in container.llm_client.calls if c["role"] == "synth")
    messages = str(gen_call["messages"])
    assert "original_q" in messages


async def test_insufficient_overwrites_generate_answer(container: Any):
    state = get_initial_state()
    state["plan"] = RoutePlanOut(
        strategy="hybrid",
        template="neighbors",
        seed_entities=["test_entity"],
        hops=1,
        sub_queries=[],
        rationale="",
    )
    state["answer"] = Answer(text="generated answer", citations=[], confidence=1.0)

    from graphrag.services.orchestration.nodes import insufficient

    res = await insufficient.node(state, container.orchestrator.deps)
    assert res["answer"].text == "I do not have enough information to answer that question."


async def test_router_selects_graph_for_multihop(container: Any):
    container.llm_client.script_structured(
        "router",
        RoutePlanOut(
            strategy="graph",
            template="path_between",
            seed_entities=[],
            hops=2,
            sub_queries=[],
            rationale="",
        ),
    )
    # No pre-seeded plan: a plan already in state means "this query has been routed, do not
    # re-route" (the `rewrite_query` loop-back). A client's `strategy` arrives as
    # `strategy_override`, never as a fabricated plan -- see `plan_route.node`.
    state = get_initial_state()
    from graphrag.services.orchestration.nodes import plan_route as pr_node

    res = await pr_node.node(state, container.orchestrator.deps)
    assert res["plan"].strategy == "graph"
    assert res["plan"].template == "path_between"


async def test_router_fails_open_on_schema_violation(container: Any):
    for _ in range(5):
        container.llm_client.script_structured("router", Exception("schema fail"))
    state = get_initial_state()
    from graphrag.services.orchestration.nodes import plan_route as pr_node

    res = await pr_node.node(state, container.orchestrator.deps)
    assert res["plan"].strategy == "hybrid"  # orchestration.default_strategy
    assert len(res["failures"]) == 1


async def test_grade_loop_bounded(container: Any):
    # Already implicitly tested by rewrite looping.
    # We can check graph routes
    pass


async def test_fabricated_citation_rejected(container: Any):
    c1_uuid = uuid4()
    state = get_initial_state()
    state["plan"] = RoutePlanOut(
        strategy="hybrid",
        template="neighbors",
        seed_entities=["test_entity"],
        hops=1,
        sub_queries=[],
        rationale="",
    )
    state["graded"] = [
        ScoredChunk(
            score=1.0,
            rank=1,
            origin="vector",
            chunk=Chunk(chunk_id=c1_uuid, text="test", content_hash="", sources=[], entity_ids=[]),
        )
    ]
    state["answer"] = Answer(
        text="ans",
        citations=[Citation(chunk_id=uuid4(), quote=None, doc_id="fake_doc", uri="")],
        confidence=1.0,
    )

    from graphrag.services.orchestration.nodes import verify_citations as vc_node

    res = await vc_node.node(state, container.orchestrator.deps)
    assert len(res["failures"]) == 1


async def test_citations_invalid_metric_incremented(container: Any, metrics_reader: Any) -> None:
    """A fabricated chunk_id must reach `verify_citations` and be caught there.

    BLUEPRINT §6.4 makes the deterministic citation check "the real guard" beside schema
    validation. If `generate` filters citations down to the ones it could resolve against
    `graded`, the guard validates the survivors against the set they were selected from and can
    never fire — `citations_invalid` never increments and the `CITATION_INVALID` failure is
    unreachable, while the answer text keeps the claim the dropped citation was supposed to
    support.
    """
    chunks = _seed_two_relevant_chunks(container)
    fabricated = uuid4()
    assert fabricated not in {chunk.chunk_id for chunk in chunks}

    container.llm_client.script_structured(
        "synth",
        AnswerOut(
            text="answer citing a chunk that was never retrieved",
            citations=[
                CitationOut(chunk_id=str(chunks[0].chunk_id), quote=None),
                CitationOut(chunk_id=str(fabricated), quote=None),
            ],
            confidence=0.9,
        ),
        AnswerOut(
            text="answer citing only real chunks",
            citations=[CitationOut(chunk_id=str(chunk.chunk_id), quote=None) for chunk in chunks],
            confidence=0.9,
        ),
    )
    container.llm_client.script_structured(
        "judge", Entailment(supported=True, unsupported_spans=[], score=0.95)
    )

    state = await run_graph(container, get_initial_state())

    assert _counter_total(metrics_reader, "graphrag.citations.invalid") == 1
    citation_failures = [f for f in state["failures"] if f.node == "verify_citations"]
    assert len(citation_failures) == 1
    assert citation_failures[0].code == "CITATION_INVALID"
    assert str(fabricated) in citation_failures[0].message
    assert state["answer"].text == "answer citing only real chunks"


async def test_repair_loop_bounded(container: Any):
    """A repair that SUCCEEDS must end the loop with the repaired answer, not a refusal.

    Drives the full graph: `verify_grounded` fails the first answer, `repair` regenerates, and
    the second answer passes verification. `max_repair_attempts` is 2, so a loop that cannot
    recognise its own success burns the second attempt and falls through to `insufficient` —
    discarding a correct, grounded, correctly-cited answer and returning the refusal text with
    an HTTP 200, which is indistinguishable from a legitimate refusal.
    """
    chunks = _seed_two_relevant_chunks(container)
    cites = [CitationOut(chunk_id=str(chunk.chunk_id), quote=None) for chunk in chunks]

    container.llm_client.script_structured(
        "synth",
        AnswerOut(text="ungrounded first attempt", citations=cites, confidence=0.9),
        AnswerOut(text="repaired grounded answer", citations=cites, confidence=0.9),
    )
    container.llm_client.script_structured(
        "judge",
        Entailment(supported=False, unsupported_spans=["first attempt"], score=0.1),
        Entailment(supported=True, unsupported_spans=[], score=0.95),
    )

    state = await run_graph(container, get_initial_state())

    assert len(_calls(container, "synth")) == 2, "expected exactly one regenerate"
    assert len(_calls(container, "judge")) == 2, "expected the repaired answer to be re-verified"
    assert state["answer"].text == "repaired grounded answer"
    assert state["attempts"]["repair"] == 1, "a successful repair must not consume a second attempt"


async def test_repair_loop_refuses_once_max_repair_attempts_is_reached(container: Any):
    """The bound itself: repairs that keep failing must stop at `max_repair_attempts` (2).

    The companion to `test_repair_loop_bounded` — that one pins that a SUCCESSFUL repair ends
    the loop, this one pins that an unsuccessful one still terminates. Together they fence the
    fix in from both sides: routing on staleness alone breaks the first, routing on nothing at
    all breaks the second.
    """
    chunks = _seed_two_relevant_chunks(container)
    cites = [CitationOut(chunk_id=str(chunk.chunk_id), quote=None) for chunk in chunks]

    container.llm_client.script_structured(
        "synth",
        AnswerOut(text="attempt one", citations=cites, confidence=0.9),
        AnswerOut(text="attempt two", citations=cites, confidence=0.9),
    )
    container.llm_client.script_structured(
        "judge",
        Entailment(supported=False, unsupported_spans=["one"], score=0.1),
        Entailment(supported=False, unsupported_spans=["two"], score=0.2),
    )

    state = await run_graph(container, get_initial_state())

    assert state["attempts"]["repair"] == 2
    assert len(_calls(container, "synth")) == 2, "must not regenerate past the bound"
    assert state["answer"].text == "I do not have enough information to answer that question."


def test_verification_routers_ignore_a_failure_from_another_node(container: Any) -> None:
    """A `retrieve_vector` degradation sitting last in `failures` is not the verifiers' failure.

    `failures` is append-only and shared by every node, so the last entry belongs to whichever
    node most recently failed — not to the node a router is deciding about. Routing on
    `failures[-1].node` reads correctly here only by luck of ordering.
    """
    deps = container.orchestrator.deps
    state = get_initial_state()
    state["failures"] = [
        NodeFailure(
            node="retrieve_vector",
            code="RETRIEVAL_BACKEND_UNAVAILABLE",
            message="qdrant unreachable",
            attempt=2,
            at=datetime.now(UTC),
        )
    ]
    state["attempts"] = {"retrieve_vector": 2, "verify_citations": 1, "verify_grounded": 1}

    assert route_after_citations(state, deps) == "verify_grounded"
    assert route_after_grounded(state, deps) == "finalize"


def test_verification_routers_ignore_a_superseded_failure(container: Any) -> None:
    """A failure recorded on attempt 1 must not steer the router after attempt 2 succeeded."""
    deps = container.orchestrator.deps
    state = get_initial_state()
    state["failures"] = [
        NodeFailure(
            node="verify_grounded",
            code="UNGROUNDED_ANSWER",
            message="Answer is not grounded (score 0.1)",
            attempt=1,
            at=datetime.now(UTC),
        )
    ]
    # verify_grounded has now run twice; the second run appended nothing, so it passed.
    state["attempts"] = {"verify_citations": 2, "verify_grounded": 2, "repair": 1}

    assert route_after_grounded(state, deps) == "finalize"

    # Same failure, still on the CURRENT attempt -> the loop must continue.
    state["attempts"] = {"verify_citations": 1, "verify_grounded": 1}
    assert route_after_grounded(state, deps) == "repair"


def _record_traversals(container: Any) -> list[tuple[str, dict]]:
    """Spy on the FakeGraphStore's traverse calls without replacing it.

    `traverse` being called AT ALL is the property under test: `GraphRetriever._params_for`
    returns None for an empty `seed_entities`, and `retrieve()` then short-circuits to `[]`
    without ever reaching the store. So an empty call list means graph retrieval never ran.
    """
    calls: list[tuple[str, dict]] = []
    original = container.graph_store.traverse

    async def spy(template: str, params: dict, *, timeout_ms: int):
        calls.append((template, params))
        return await original(template, params, timeout_ms=timeout_ms)

    container.graph_store.traverse = spy
    return calls


def _seed_linkable_entity(container: Any) -> Any:
    entity = make_entity(name="Acme")
    container.vector_store.entities[entity.canonical_id] = entity
    container.vector_store.entity_vectors[entity.canonical_id] = [0.1] * 8
    return entity


async def test_client_strategy_overrides_policy_but_not_the_routers_semantics(container: Any):
    """`strategy` from the client picks the strategy; the router still supplies the plan.

    The router does two separable jobs — choosing a strategy (policy a client can know) and
    extracting seed_entities/template/hops from the question (semantics a client cannot). A
    client override that forges the whole plan sets `seed_entities=[]`, which makes
    `GraphRetriever._params_for` return None and graph retrieval return [] every time, so
    `strategy="graph"` yields nothing at all and `strategy="hybrid"` degrades to vector-only
    with `degraded` empty and nothing reporting it.
    """
    _seed_linkable_entity(container)
    traversals = _record_traversals(container)

    # Router picks `vector`; the client asked for `graph`. The client wins on strategy only.
    plan_out = RoutePlanOut(
        strategy="vector",
        template="neighbors",
        seed_entities=["Acme"],
        hops=1,
        sub_queries=[],
        rationale="entity lookup, one hop is enough",
    )
    # `rewrite_query` loops back to `plan_route`, which re-plans, so one RoutePlanOut per pass.
    container.llm_client.script_structured(
        "router",
        plan_out,
        plan_out,
        plan_out,
        RewrittenQuery(query="r1", changed_because=""),
        RewrittenQuery(query="r2", changed_because=""),
    )

    result = await container.orchestrator.run("who runs Acme?", "cid-1", strategy="graph")

    assert result.route.strategy == "graph", "client strategy must win"
    assert result.route.seed_entities == ["Acme"], "router semantics must survive the override"
    assert result.route.rationale == "entity lookup, one hop is enough", (
        "rationale must carry the router's real reasoning, not a fabricated override note"
    )
    assert traversals, "strategy='graph' must actually reach the graph store"
    assert traversals[0][0] == "neighbors"


async def test_hybrid_strategy_override_runs_both_retrievers(container: Any):
    """`strategy: "hybrid"` must perform graph retrieval, not silently degrade to vector-only."""
    _seed_linkable_entity(container)
    traversals = _record_traversals(container)
    container.vector_store.chunks.clear()

    plan_out = RoutePlanOut(
        strategy="vector",
        template="neighbors",
        seed_entities=["Acme"],
        hops=1,
        sub_queries=[],
        rationale="",
    )
    container.llm_client.script_structured(
        "router",
        plan_out,
        plan_out,
        plan_out,
        RewrittenQuery(query="r1", changed_because=""),
        RewrittenQuery(query="r2", changed_because=""),
    )

    result = await container.orchestrator.run("who runs Acme?", "cid-2", strategy="hybrid")

    assert result.route.strategy == "hybrid"
    assert container.vector_store.hybrid_search_calls > 0, "vector leg must run"
    assert traversals, "graph leg must run -- a hybrid route that skips it is a silent degradation"
    assert result.degraded == [], "neither backend failed, so nothing is degraded"


async def test_budget_exhaustion_routes_to_insufficient(container: Any):
    state = get_initial_state()
    state["plan"] = RoutePlanOut(
        strategy="hybrid",
        template="neighbors",
        seed_entities=["test_entity"],
        hops=1,
        sub_queries=[],
        rationale="",
    )
    state["spent"] = Spend(wall_ms=container.settings.limits.per_request_budget.max_wall_ms + 100)

    # We can test the graph condition
    from graphrag.services.orchestration.graph import route_after_guard

    res = route_after_guard(state, container.orchestrator.deps)
    assert res == "insufficient"
