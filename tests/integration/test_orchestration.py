import pytest

from graphrag.apps.api.main import Container
from graphrag.config.settings import Settings
from tests.integration import namespaces as ns

pytestmark = pytest.mark.integration


async def _drop_qdrant(settings: Settings) -> None:
    """Drop the collections `Container.create` made. Goes through `drop_collections`, which
    refuses any name this run did not mint."""
    from qdrant_client import AsyncQdrantClient

    from tests.integration.conftest import drop_collections

    admin = AsyncQdrantClient(url=settings.stores.qdrant.url, prefer_grpc=False, timeout=10)
    try:
        await drop_collections(
            admin, settings.retrieval.vector.collection, settings.resolution.collection
        )
    finally:
        await admin.close()


@pytest.fixture
async def container(_pg_database: str) -> Container:
    # `ns.namespaced`, not `get_settings()`. `Container.create` calls `ensure_collections()` and
    # `ensure_schema()` on whatever it is handed, so a production `Settings` here pointed the
    # whole orchestration suite at the real `chunks`/`entities` collections and the real graph.
    # These tests assert on degradation and correlation-id handling, not on corpus content, so
    # they are unaffected by moving to empty per-run namespaces.
    from unittest.mock import patch

    from graphrag.services.orchestration.schemas import (
        AnswerOut,
        CitationOut,
        Entailment,
        RelevanceGradeBatch,
        RewrittenQuery,
        RoutePlanOut,
    )
    from tests.fakes import FakeLLMClient

    fake_llm = FakeLLMClient()
    fake_llm.script_structured(
        "router",
        *(RoutePlanOut(strategy="hybrid", hops=1, rationale="test") for _ in range(50)),
        *(RewrittenQuery(query="what", changed_because="test") for _ in range(50)),
    )
    # Give synth and grader some default responses since multiple tests hit them
    fake_llm.script_structured(
        "synth",
        *(
            AnswerOut(
                text="test answer", citations=[CitationOut(chunk_id="test_id")], confidence=1.0
            )
            for _ in range(50)
        ),
    )
    fake_llm.script_structured(
        "grader",
        *(RelevanceGradeBatch(grades=[]) for _ in range(50)),
    )
    fake_llm.script_structured(
        "judge",
        *(Entailment(supported=True, score=1.0) for _ in range(50)),
    )

    settings = ns.namespaced(Settings(), local="orch")
    with patch("graphrag.apps.api.main.LiteLLMClient", return_value=fake_llm):
        c = await Container.create(settings)
    try:
        yield c
    finally:
        await c.aclose()
        await _drop_qdrant(settings)


@pytest.mark.asyncio
async def test_degrades_without_graph(container: Container, monkeypatch: pytest.MonkeyPatch):
    # Change the neo4j password to something wrong to simulate failure
    # Actually wait, graph retrieval happens during route. We can just shut down neo4j?
    # No, we can't shut down neo4j from here. But we can monkeypatch `graph_store.traverse` to raise GraphBackendUnavailable!
    from unittest.mock import patch

    from graphrag.core.errors import GraphBackendUnavailable
    from graphrag.core.ids import new_correlation_id

    with patch(
        "graphrag.services.retrieval.graph.GraphRetriever.retrieve",
        side_effect=GraphBackendUnavailable("test"),
    ):
        result = await container.orchestrator.run("what?", new_correlation_id())

    assert "graph" in result.degraded


@pytest.mark.asyncio
async def test_degrades_without_vector_store(container: Container):
    from unittest.mock import patch

    from graphrag.core.errors import RetrievalBackendUnavailable
    from graphrag.core.ids import new_correlation_id

    with patch(
        "graphrag.services.retrieval.vector.VectorRetriever.retrieve",
        side_effect=RetrievalBackendUnavailable("test"),
    ):
        result = await container.orchestrator.run("what?", new_correlation_id())

    assert "vector" in result.degraded


@pytest.mark.asyncio
async def test_no_correlation_id_bleed(container: Container):
    import asyncio

    from graphrag.core.ids import new_correlation_id

    # Fire 20 concurrent queries
    coros = []
    cids = []
    for i in range(20):
        cid = new_correlation_id()
        cids.append(cid)
        coros.append(container.orchestrator.run(f"query {i}", cid))

    results = await asyncio.gather(*coros)

    assert len(results) == 20

    # Assert each query state maintained its distinct correlation id
    # Since our orchestrator returns a QueryState, we can check it
    for i, result in enumerate(results):
        assert result.correlation_id == cids[i]


# ---------------------------------------------------------------------------
# Hybrid strategy override, against the real stores.
#
# `tests/unit/test_orchestration_graph.py::test_hybrid_strategy_override_runs_both_retrievers`
# proves the WIRING -- that the router's plan is overridden and both retrieval nodes are
# entered -- against fakes and a scripted traversal. It cannot prove the BEHAVIOUR: the bug it
# was written for was graph retrieval returning [] for every overridden query while `degraded`
# stayed empty, which reads as "hybrid worked, the graph just had nothing to say". Only real
# Neo4j traversal + real entity linking through Qdrant can tell those two apart, so this test
# runs the same override end to end and asserts on the content that comes back.
# ---------------------------------------------------------------------------

_HYBRID_DOC_ID = "doc-hybrid-override"
_HYBRID_URI = "file:///doc-hybrid-override.txt"
# Seeded into Neo4j ONLY -- never into the Qdrant chunks collection. Anything from these two
# texts that reaches the answer can only have arrived through the graph leg.
_GRAPH_ONLY_TEXTS = (
    "Acme Robotics was founded in Nagoya by Mariko Endo, who still chairs its board.",
    "Acme Robotics acquired Helios Actuators in 2019 to supply its assembly arms.",
)
# Seeded into Qdrant ONLY, so the vector leg has something of its own to return and the run is
# a real fusion of two populated legs rather than a graph-only query wearing a hybrid label.
_VECTOR_ONLY_TEXT = "The Acme Robotics staff cafeteria on the third floor reopens on Monday."


@pytest.fixture
async def hybrid_container(clean_neo4j, _pg_database: str):
    """A real `Container` on this run's own Qdrant collections, with a scripted LLM.

    Every store it touches is namespaced by `tests/integration/namespaces.py`, so this test
    neither reads nor damages an ingested corpus, and every chunk the query can possibly see is
    one this fixture put there.
    """
    import uuid
    from unittest.mock import patch

    from graphrag.core.ids import chunk_id as compute_chunk_id
    from graphrag.services.orchestration.schemas import (
        AnswerOut,
        CitationOut,
        Entailment,
        RelevanceGrade,
        RelevanceGradeBatch,
        RewrittenQuery,
        RoutePlanOut,
    )
    from tests.fakes import FakeLLMClient

    settings = ns.namespaced(Settings(), local=uuid.uuid4().hex[:8])

    # Chunk ids are content-addressed, so the grader's and synth's scripted responses can name
    # the exact chunks this test is about before anything is written anywhere.
    graph_chunk_ids = [compute_chunk_id(text) for text in _GRAPH_ONLY_TEXTS]
    vector_chunk_id = compute_chunk_id(_VECTOR_ONLY_TEXT)

    fake_llm = FakeLLMClient()
    # strategy="vector" on purpose: the run passes strategy="hybrid", so a plan that comes back
    # marked hybrid can only have been overridden.
    fake_llm.script_structured(
        "router",
        *(
            RoutePlanOut(
                strategy="vector",
                template="neighbors",
                seed_entities=["Acme Robotics"],
                hops=1,
                sub_queries=[],
                rationale="test",
            )
            for _ in range(4)
        ),
        *(
            RewrittenQuery(query="who chairs Acme Robotics?", changed_because="test")
            for _ in range(4)
        ),
    )
    # A grade for EVERY fused chunk, including the vector-only one. `grade_context` treats a
    # grade count that does not match the chunk count as a failure -- it keeps the whole batch
    # and marks the run degraded -- because a truncated grader response is otherwise
    # indistinguishable from one that deliberately judged a chunk irrelevant. Grading only the
    # two graph chunks (which this fixture used to do) is therefore a degraded run under those
    # semantics, not a normal one, and `test_hybrid_override_returns_real_graph_content` asserts
    # on a clean `degraded`. The cafeteria text really is irrelevant to "who chairs Acme
    # Robotics?", so saying so explicitly is also the more honest script.
    fake_llm.script_structured(
        "grader",
        *(
            RelevanceGradeBatch(
                grades=[
                    RelevanceGrade(chunk_id=str(cid), relevant=True, reason="graph evidence")
                    for cid in graph_chunk_ids
                ]
                + [
                    RelevanceGrade(
                        chunk_id=str(vector_chunk_id),
                        relevant=False,
                        reason="cafeteria hours, not board membership",
                    )
                ]
            )
            for _ in range(8)
        ),
    )
    fake_llm.script_structured(
        "synth",
        *(
            AnswerOut(
                text="Mariko Endo chairs the board of Acme Robotics.",
                citations=[CitationOut(chunk_id=str(graph_chunk_ids[0]), quote=None)],
                confidence=0.9,
            )
            for _ in range(4)
        ),
    )
    fake_llm.script_structured(
        "judge",
        *(Entailment(supported=True, score=1.0) for _ in range(4)),
    )

    with patch("graphrag.apps.api.main.LiteLLMClient", return_value=fake_llm):
        container = await Container.create(settings)
    try:
        yield container
    finally:
        await container.aclose()
        await _drop_qdrant(settings)


async def _seed_hybrid_corpus(container: Container) -> list:
    """Two chunks reachable only through Neo4j, one reachable only through Qdrant, and the
    entities the real `EntityLinker` has to resolve "Acme Robotics" against."""
    from datetime import UTC, datetime

    from graphrag.core.ids import chunk_id as compute_chunk_id
    from graphrag.core.ids import content_hash
    from graphrag.core.models import Chunk, EntityType, SourceRef
    from tests.factories import make_entity, make_relation

    graph_chunks = [
        Chunk(
            chunk_id=compute_chunk_id(text),
            text=text,
            content_hash=content_hash(text),
            sources=[
                SourceRef(
                    doc_id=_HYBRID_DOC_ID,
                    uri=_HYBRID_URI,
                    page=None,
                    char_start=0,
                    char_end=len(text),
                    ingested_at=datetime(2024, 1, 1, tzinfo=UTC),
                )
            ],
            entity_ids=[],
        )
        for text in _GRAPH_ONLY_TEXTS
    ]

    acme = make_entity(name="Acme Robotics", type=EntityType.ORG)
    mariko = make_entity(name="Mariko Endo", type=EntityType.PERSON)
    helios = make_entity(name="Helios Actuators", type=EntityType.ORG)
    entities = [acme, mariko, helios]

    await container.graph_store.upsert_document(
        _HYBRID_DOC_ID, _HYBRID_URI, "Acme Robotics", sha256="sha-hybrid"
    )
    await container.graph_store.upsert_chunks(_HYBRID_DOC_ID, graph_chunks)
    await container.graph_store.upsert_entities(entities)
    await container.graph_store.upsert_relations(
        [
            make_relation(
                src_id=acme.canonical_id,
                dst_id=mariko.canonical_id,
                type="CHAIRED_BY",
                chunk_id=graph_chunks[0].chunk_id,
                doc_id=_HYBRID_DOC_ID,
                evidence_span=_GRAPH_ONLY_TEXTS[0],
            ),
            make_relation(
                src_id=acme.canonical_id,
                dst_id=helios.canonical_id,
                type="ACQUIRED",
                chunk_id=graph_chunks[1].chunk_id,
                doc_id=_HYBRID_DOC_ID,
                evidence_span=_GRAPH_ONLY_TEXTS[1],
            ),
        ]
    )

    # Entity vectors are embedded from `name_normalized`, exactly as `worker/tasks/resolve.py`
    # writes them -- the linker is only as real as the vectors it searches.
    entity_vectors = await container.embedder.embed_dense(
        [entity.name_normalized for entity in entities]
    )
    await container.vector_store.upsert_entities(entities, entity_vectors)

    vector_chunk = Chunk(
        chunk_id=compute_chunk_id(_VECTOR_ONLY_TEXT),
        text=_VECTOR_ONLY_TEXT,
        content_hash=content_hash(_VECTOR_ONLY_TEXT),
        sources=[
            SourceRef(
                doc_id="doc-hybrid-vector-only",
                uri="file:///doc-hybrid-vector-only.txt",
                page=None,
                char_start=0,
                char_end=len(_VECTOR_ONLY_TEXT),
                ingested_at=datetime(2024, 1, 1, tzinfo=UTC),
            )
        ],
        entity_ids=[],
    )
    [dense] = await container.embedder.embed_dense([vector_chunk.text])
    [sparse] = await container.embedder.embed_sparse([vector_chunk.text])
    await container.vector_store.upsert_chunks([vector_chunk], [dense], [sparse])

    return graph_chunks


@pytest.mark.asyncio
async def test_hybrid_override_returns_real_graph_content(
    hybrid_container: Container, monkeypatch: pytest.MonkeyPatch
):
    """`strategy: "hybrid"` must come back with graph content, and `degraded` must stay empty.

    The regression this guards is not "the graph retriever was never called" -- it is "it was
    called, returned [], and nothing anywhere said so". So the empty `degraded` list is only
    meaningful next to a non-empty result: asserted together, they say the graph leg ran, found
    something, and no backend quietly dropped out.
    """
    from graphrag.core.ids import new_correlation_id

    graph_chunks = await _seed_hybrid_corpus(hybrid_container)
    expected_texts = {chunk.text for chunk in graph_chunks}

    deps = hybrid_container.orchestrator.deps
    real_graph_retrieve = deps.graph.retrieve
    real_vector_retrieve = deps.vector.retrieve
    graph_results: list = []
    vector_results: list = []

    async def recording_graph_retrieve(plan, max_hops):
        paths = await real_graph_retrieve(plan, max_hops)
        graph_results.append(paths)
        return paths

    async def recording_vector_retrieve(query, top_k):
        hits = await real_vector_retrieve(query, top_k)
        vector_results.append(hits)
        return hits

    monkeypatch.setattr(deps.graph, "retrieve", recording_graph_retrieve)
    monkeypatch.setattr(deps.vector, "retrieve", recording_vector_retrieve)

    result = await hybrid_container.orchestrator.run(
        "who chairs Acme Robotics?", new_correlation_id(), strategy="hybrid"
    )

    assert result.route is not None
    assert result.route.strategy == "hybrid", "the client override must survive the router"

    assert graph_results, "the graph leg never ran"
    paths = graph_results[0]
    assert paths, "graph retrieval returned no paths -- the override degraded to vector-only"
    retrieved_texts = {chunk.text for path in paths for chunk in path.chunks}
    assert retrieved_texts == expected_texts, (
        "graph paths came back without their hydrated chunk text -- a non-empty path list with "
        "nothing readable in it is the same outage wearing a different shape"
    )

    assert vector_results and vector_results[0], "the vector leg never returned anything"

    assert result.degraded == [], (
        "neither backend failed, so nothing may be marked degraded -- an empty `degraded` "
        "alongside empty graph results was the original silent-degradation symptom"
    )

    cited_ids = {citation.chunk_id for citation in result.answer.citations}
    assert cited_ids & {chunk.chunk_id for chunk in graph_chunks}, (
        "the answer cites nothing that came from the graph"
    )
    [cited] = [c for c in result.answer.citations if c.chunk_id == graph_chunks[0].chunk_id]
    assert cited.doc_id == _HYBRID_DOC_ID
    assert cited.uri == _HYBRID_URI
