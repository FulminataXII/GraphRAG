"""`GraphRetriever` unit tests (fakes only). See BLUEPRINT §6.3.

`tests.fakes.FakeGraphStore.traverse` always returns `[]` (a stub -- BO-08 never needed it to do
more), which makes GraphRetriever's path filtering/ranking/truncation logic untestable at the
unit level. That coverage lives in `tests/integration/test_retrieval_graph.py` against a real
Neo4j, including this BO's `test_graph_hydrates_from_neo4j` gate. What belongs here is the one
behavior provable without any real traversal result: a query whose seeds link to nothing never
even attempts one.
"""

from __future__ import annotations

from graphrag.core.models import RoutePlan
from graphrag.services.retrieval.graph import GraphRetriever
from graphrag.services.retrieval.linker import EntityLinker
from tests.fakes import FakeEmbedder, FakeGraphStore, FakeVectorStore


async def test_graph_retriever_returns_empty_when_nothing_links(settings) -> None:
    """No seed entity links -> [] immediately, without ever calling `graph_store.traverse` --
    proven by poisoning `graph_store` (`.fail = True`, same convention as every other
    `Fake*.fail` flag): if `retrieve()` touched it anyway, this would raise instead of
    returning cleanly."""
    graph_store = FakeGraphStore()
    graph_store.fail = True
    vector_store = FakeVectorStore()  # empty -- nothing for the linker to find
    linker = EntityLinker(embedder=FakeEmbedder(), vector_store=vector_store)
    retriever = GraphRetriever(
        graph_store=graph_store,
        vector_store=vector_store,
        linker=linker,
        entity_link_top_k=settings.retrieval.graph.entity_link_top_k,
        entity_link_min_score=settings.retrieval.graph.entity_link_min_score,
        max_degree_per_hop=settings.retrieval.graph.max_degree_per_hop,
        timeout_ms=settings.retrieval.graph.timeout_ms,
        max_paths=settings.retrieval.graph.max_paths,
        hydrate_from=settings.retrieval.graph.hydrate_from,
    )
    plan = RoutePlan(
        strategy="graph",
        seed_entities=["Some Entity Nobody Indexed"],
        hops=2,
        sub_queries=[],
        rationale="test",
    )

    result = await retriever.retrieve(plan, max_hops=2)

    assert result == []
