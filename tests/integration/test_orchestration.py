import pytest

from graphrag.apps.api.main import Container
from graphrag.config.settings import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
async def container(monkeypatch) -> Container:
    from tests.integration.conftest import POSTGRES_DSN_LOCAL

    monkeypatch.setenv("GRAPHRAG_SECRETS__POSTGRES_DSN", POSTGRES_DSN_LOCAL)

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

    with patch("graphrag.apps.api.main.LiteLLMClient", return_value=fake_llm):
        settings = get_settings()
        c = await Container.create(settings)
        yield c
        await c.aclose()


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
