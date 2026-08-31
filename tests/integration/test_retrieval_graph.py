"""`GraphRetriever` against real Neo4j. See BLUEPRINT §6.3 / BUILD_ORDER BO-09.

Unit-level behavior (returns [] when nothing links) is covered by
`tests/unit/test_retrieval_graph.py`, which needs no live backend.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from graphrag.adapters.neo4j_store import Neo4jGraphStore
from graphrag.config.settings import Settings
from graphrag.core.ids import chunk_id as compute_chunk_id
from graphrag.core.ids import content_hash
from graphrag.core.models import Chunk, RoutePlan, SourceRef
from graphrag.services.retrieval.graph import GraphRetriever
from tests.factories import make_entity, make_relation
from tests.fakes import FakeVectorStore
from tests.unit._settings_helpers import set_required_secrets

pytestmark = pytest.mark.integration


@pytest.fixture
def graph_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    set_required_secrets(monkeypatch)
    return Settings()


@pytest.fixture
def graph_store(clean_neo4j: Any, graph_settings: Settings) -> Neo4jGraphStore:
    return Neo4jGraphStore(clean_neo4j, graph_settings)


class _AllEntitiesLinker:
    """Stands in for `EntityLinker` -- always "links" every seed string to a fixed, pre-seeded
    entity, so this test can control seed resolution directly instead of routing it through a
    real Qdrant entities collection (which `EntityLinker` itself already covers at the unit
    level via `_ScoredEntityStore`, `tests/unit/test_retrieval_linker.py`)."""

    def __init__(self, entities: list[Any]) -> None:
        self._entities = entities

    async def link(self, query: str, *, top_k: int, min_score: float) -> list[Any]:
        return self._entities[:top_k]


async def test_graph_hydrates_from_neo4j(
    graph_store: Neo4jGraphStore, graph_settings: Settings
) -> None:
    """No Qdrant call occurs when `hydrate_from=neo4j` (BUILD_ORDER gate). `vector_store` is
    poisoned (`.fail = True`, the established `Fake*.fail` convention) so ANY call to it —
    `get_chunks` included — raises; `retrieve()` completing without raising, with the right
    chunk text attached to the returned `GraphPath.chunks`, proves hydration went through the
    graph store alone.
    """
    assert graph_settings.retrieval.graph.hydrate_from == "neo4j"  # the config this BO ships

    text = "graph-hydrated chunk text for the vector-outage fallback"
    chunk = Chunk(
        chunk_id=compute_chunk_id(text),
        text=text,
        content_hash=content_hash(text),
        sources=[
            SourceRef(
                doc_id="doc-hydrate",
                uri="file:///doc-hydrate.txt",
                page=None,
                char_start=0,
                char_end=len(text),
                ingested_at=datetime(2024, 1, 1, tzinfo=UTC),
            )
        ],
        entity_ids=[],
    )
    await graph_store.upsert_document(
        "doc-hydrate", "file:///doc-hydrate.txt", "doc-hydrate", sha256="sha"
    )
    await graph_store.upsert_chunks("doc-hydrate", [chunk])

    entity_a = make_entity(name="Hydrate A", canonical_id=uuid4())
    entity_b = make_entity(name="Hydrate B", canonical_id=uuid4())
    await graph_store.upsert_entities([entity_a, entity_b])
    await graph_store.upsert_relations(
        [
            make_relation(
                src_id=entity_a.canonical_id,
                dst_id=entity_b.canonical_id,
                chunk_id=chunk.chunk_id,
                doc_id="doc-hydrate",
            )
        ]
    )

    poisoned_vector_store = FakeVectorStore()
    poisoned_vector_store.fail = True
    retriever = GraphRetriever(
        graph_store=graph_store,
        vector_store=poisoned_vector_store,
        linker=_AllEntitiesLinker([entity_a]),  # type: ignore[arg-type]
        entity_link_top_k=graph_settings.retrieval.graph.entity_link_top_k,
        entity_link_min_score=graph_settings.retrieval.graph.entity_link_min_score,
        max_degree_per_hop=graph_settings.retrieval.graph.max_degree_per_hop,
        timeout_ms=graph_settings.retrieval.graph.timeout_ms,
        max_paths=graph_settings.retrieval.graph.max_paths,
        hydrate_from=graph_settings.retrieval.graph.hydrate_from,
    )
    plan = RoutePlan(
        strategy="graph", seed_entities=["Hydrate A"], hops=2, sub_queries=[], rationale="test"
    )

    paths = await retriever.retrieve(plan, max_hops=2)

    assert paths, "traversal should have found the RELATES edge seeded above"
    hydrated_chunk_ids = {chunk.chunk_id for path in paths for chunk in path.chunks}
    assert chunk.chunk_id in hydrated_chunk_ids
    [hydrated] = [c for path in paths for c in path.chunks if c.chunk_id == chunk.chunk_id]
    assert hydrated.text == text
