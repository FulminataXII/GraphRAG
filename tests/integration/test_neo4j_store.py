"""`Neo4jGraphStore` against a real Neo4j (`make up`). See BLUEPRINT §5.3 / BUILD_ORDER BO-08.

Vacuity guards (BUILD_ORDER's own warning): BO-07 extracts relations and computes alias edges
but persists neither -- an empty database trivially satisfies "every relation has provenance"
and "zero new relationships on re-run". Every test below that could pass vacuously asserts a
NON-ZERO baseline first.
"""

from __future__ import annotations

import itertools
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from graphrag.adapters.neo4j_store import Neo4jGraphStore
from graphrag.apps.api.main import Container, ReadyzProber
from graphrag.apps.worker.tasks.extract import extract_entities
from graphrag.apps.worker.tasks.resolve import resolve_entities
from graphrag.config.settings import Settings
from graphrag.core.events import ExtractEntitiesPayload, JobEnvelope, ResolveEntitiesPayload
from graphrag.core.ids import chunk_id as compute_chunk_id
from graphrag.core.ids import content_hash
from graphrag.core.models import Chunk, DocumentStatus, EntityType, SourceRef, SparseVector
from graphrag.services.ingestion.chunker import chunk_document
from graphrag.services.ingestion.parser import DocumentParser
from graphrag.services.orchestration.schemas import EntityExtraction, MentionOut, RelationOut
from tests.factories import make_entity, make_metrics, make_relation
from tests.fakes import (
    FakeCache,
    FakeDocumentLedger,
    FakeEmbedder,
    FakeJobQueue,
    FakeLLMClient,
    FakeSourceRegistry,
    FakeVectorStore,
)
from tests.unit._settings_helpers import set_required_secrets

pytestmark = pytest.mark.integration

_CORPUS_DOC = (
    Path(__file__).resolve().parents[2]
    / "corpus"
    / "04_apple-opens-advanced-manufacturing-center-in-houston.txt"
)


@pytest.fixture
def graph_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    set_required_secrets(monkeypatch)
    return Settings()


@pytest.fixture
def graph_store(clean_neo4j: Any, graph_settings: Settings) -> Neo4jGraphStore:
    return Neo4jGraphStore(clean_neo4j, graph_settings)


def _build_chunks(doc_id: str, uri: str, text: str, settings: Settings) -> list[Chunk]:
    """Mirrors `IngestionService.ingest()`'s own chunk-building (content-addressed ids, one
    SourceRef per chunk) without pulling in Postgres/Qdrant -- this BO's tests are about the
    extract -> resolve -> graph handoff, which needs real `Chunk` objects but no vector/ledger
    persistence to prove."""
    parsed = DocumentParser().parse(text.encode("utf-8"), "text/plain")
    specs = chunk_document(
        parsed,
        chunk_size=settings.ingestion.chunk_size,
        chunk_overlap=settings.ingestion.chunk_overlap,
        min_chunk_chars=settings.ingestion.min_chunk_chars,
    )
    now = datetime(2024, 1, 1, tzinfo=UTC)
    by_id: dict[UUID, Chunk] = {}
    for spec in specs:
        cid = compute_chunk_id(spec.text)
        by_id.setdefault(
            cid,
            Chunk(
                chunk_id=cid,
                text=spec.text,
                content_hash=content_hash(spec.text),
                sources=[
                    SourceRef(
                        doc_id=doc_id,
                        uri=uri,
                        page=spec.page,
                        char_start=spec.char_start,
                        char_end=spec.char_end,
                        ingested_at=now,
                    )
                ],
                entity_ids=[],
            ),
        )
    return list(by_id.values())


async def _seed_document(graph_store: Neo4jGraphStore, doc_id: str, chunks: list[Chunk]) -> None:
    await graph_store.upsert_document(
        doc_id, f"file:///{doc_id}.txt", doc_id, sha256="sha-" + doc_id
    )
    await graph_store.upsert_chunks(doc_id, chunks)


async def _counts(driver: Any) -> tuple[int, int]:
    records, _summary, _keys = await driver.execute_query(
        "MATCH (n) OPTIONAL MATCH (n)-[r]->() RETURN count(DISTINCT n) AS nodes, count(r) AS rels",
        database_="neo4j",
    )
    row = records[0]
    return row["nodes"], row["rels"]


async def test_ensure_schema_idempotent(graph_store: Neo4jGraphStore, clean_neo4j: Any) -> None:
    await graph_store.ensure_schema()
    await graph_store.ensure_schema()  # must not raise -- IF NOT EXISTS makes this idempotent

    records, _summary, _keys = await clean_neo4j.execute_query(
        "SHOW CONSTRAINTS YIELD name RETURN collect(name) AS names", database_="neo4j"
    )
    constraint_names = set(records[0]["names"])
    assert {"doc_id", "chunk_id", "entity_cid"} <= constraint_names

    records, _summary, _keys = await clean_neo4j.execute_query(
        "SHOW INDEXES YIELD name RETURN collect(name) AS names", database_="neo4j"
    )
    assert "entity_norm" in set(records[0]["names"])


async def test_neo4j_stores_full_chunk_text(
    graph_store: Neo4jGraphStore, graph_settings: Settings
) -> None:
    long_text = "Apple announced a new product today. " * 60  # well over any "preview" length
    chunk = Chunk(
        chunk_id=compute_chunk_id(long_text),
        text=long_text,
        content_hash=content_hash(long_text),
        sources=[
            SourceRef(
                doc_id="doc-full-text",
                uri="file:///doc-full-text.txt",
                page=None,
                char_start=0,
                char_end=len(long_text),
                ingested_at=datetime(2024, 1, 1, tzinfo=UTC),
            )
        ],
        entity_ids=[],
    )
    await _seed_document(graph_store, "doc-full-text", [chunk])

    [fetched] = await graph_store.get_chunks([chunk.chunk_id])
    assert fetched.text == long_text
    assert len(fetched.text) == len(long_text)


async def test_get_chunks_returns_sources(graph_store: Neo4jGraphStore) -> None:
    chunk = Chunk(
        chunk_id=compute_chunk_id("sourced chunk text"),
        text="sourced chunk text",
        content_hash=content_hash("sourced chunk text"),
        sources=[
            SourceRef(
                doc_id="doc-src",
                uri="file:///doc-src.txt",
                page=3,
                char_start=10,
                char_end=29,
                ingested_at=datetime(2024, 6, 1, 12, 0, tzinfo=UTC),
            )
        ],
        entity_ids=[],
    )
    await _seed_document(graph_store, "doc-src", [chunk])

    [fetched] = await graph_store.get_chunks([chunk.chunk_id])
    assert fetched.text == "sourced chunk text"
    assert len(fetched.sources) == 1
    [source] = fetched.sources
    assert source.doc_id == "doc-src"
    assert source.uri == "file:///doc-src.txt"
    assert source.page == 3
    assert source.char_start == 10
    assert source.char_end == 29


async def test_get_chunks_fallback_path(graph_store: Neo4jGraphStore) -> None:
    """`get_chunks` is both the graph retrieval hydration path AND the vector-outage fallback
    (BLUEPRINT §3.5). This test constructs no Qdrant client anywhere in scope -- the only way it
    can pass is if `Neo4jGraphStore.get_chunks` never needs one."""
    chunk = Chunk(
        chunk_id=compute_chunk_id("fallback text lives only in the graph"),
        text="fallback text lives only in the graph",
        content_hash=content_hash("fallback text lives only in the graph"),
        sources=[
            SourceRef(
                doc_id="doc-fallback",
                uri="file:///doc-fallback.txt",
                page=None,
                char_start=0,
                char_end=38,
                ingested_at=datetime(2024, 1, 1, tzinfo=UTC),
            )
        ],
        entity_ids=[],
    )
    await _seed_document(graph_store, "doc-fallback", [chunk])

    [fetched] = await graph_store.get_chunks([chunk.chunk_id])
    assert fetched.text == "fallback text lives only in the graph"


async def test_graph_upsert_idempotent(graph_store: Neo4jGraphStore, clean_neo4j: Any) -> None:
    chunk = Chunk(
        chunk_id=compute_chunk_id("idempotent upsert text"),
        text="idempotent upsert text",
        content_hash=content_hash("idempotent upsert text"),
        sources=[
            SourceRef(
                doc_id="doc-idem",
                uri="file:///doc-idem.txt",
                page=None,
                char_start=0,
                char_end=23,
                ingested_at=datetime(2024, 1, 1, tzinfo=UTC),
            )
        ],
        entity_ids=[],
    )
    await _seed_document(graph_store, "doc-idem", [chunk])
    entity = make_entity(name="Idem Corp")
    await graph_store.upsert_entities([entity])
    relation = make_relation(
        src_id=entity.canonical_id,
        dst_id=entity.canonical_id,
        chunk_id=chunk.chunk_id,
        doc_id="doc-idem",
    )
    await graph_store.upsert_relations([relation])

    before = await _counts(clean_neo4j)
    assert before[0] > 0  # non-zero baseline -- zero-to-zero is not idempotence
    assert before[1] > 0

    await _seed_document(graph_store, "doc-idem", [chunk])
    await graph_store.upsert_entities([entity])
    await graph_store.upsert_relations([relation])

    after = await _counts(clean_neo4j)
    assert after == before


async def test_every_relation_has_provenance(
    graph_store: Neo4jGraphStore, clean_neo4j: Any
) -> None:
    entity_a = make_entity(name="Provenance A")
    entity_b = make_entity(name="Provenance B")
    await graph_store.upsert_entities([entity_a, entity_b])
    await graph_store.upsert_relations(
        [
            make_relation(
                src_id=entity_a.canonical_id,
                dst_id=entity_b.canonical_id,
                chunk_id=uuid4(),
                doc_id="doc-prov",
            )
        ]
    )

    records, _summary, _keys = await clean_neo4j.execute_query(
        "MATCH ()-[r:RELATES]->() RETURN count(r) AS n", database_="neo4j"
    )
    total = records[0]["n"]
    assert total > 0  # non-empty precondition -- this is the state BO-07 alone leaves behind

    records, _summary, _keys = await clean_neo4j.execute_query(
        "MATCH ()-[r:RELATES]->() WHERE r.chunk_id IS NULL RETURN count(r) AS n",
        database_="neo4j",
    )
    assert records[0]["n"] == 0


async def test_alias_edges_persisted(graph_store: Neo4jGraphStore, clean_neo4j: Any) -> None:
    canonical = make_entity(name="Canonical Name")
    await graph_store.upsert_entities([canonical])
    loser_id = uuid4()
    await graph_store.add_alias(loser_id, canonical.canonical_id, 0.91, "cluster")

    records, _summary, _keys = await clean_neo4j.execute_query(
        "MATCH (a:Entity)-[r:ALIAS_OF]->(c:Entity) RETURN count(r) AS n", database_="neo4j"
    )
    assert records[0]["n"] > 0

    records, _summary, _keys = await clean_neo4j.execute_query(
        "MATCH (a:Entity {canonical_id: $alias_id}) RETURN a",
        database_="neo4j",
        parameters_={"alias_id": str(loser_id)},
    )
    assert len(records) == 1  # the loser was never deleted


async def test_traverse_populates_cypher_templates(graph_store: Neo4jGraphStore) -> None:
    """Sanity check that every enabled template key actually runs against a real database
    (not just that the key is whitelisted, which the unit suite already covers)."""
    entity_a = make_entity(name="Template A", canonical_id=uuid4())
    entity_b = make_entity(name="Template B", canonical_id=uuid4())
    await graph_store.upsert_entities([entity_a, entity_b])
    shared_chunk_id = uuid4()
    await graph_store.upsert_relations(
        [
            make_relation(
                src_id=entity_a.canonical_id,
                dst_id=entity_b.canonical_id,
                type="WORKS_WITH",
                chunk_id=shared_chunk_id,
                doc_id="doc-templates",
            )
        ]
    )

    results = {}
    results["neighbors"] = await graph_store.traverse(
        "neighbors", {"entity_id": str(entity_a.canonical_id)}, timeout_ms=3000
    )
    results["path_between"] = await graph_store.traverse(
        "path_between",
        {"src_id": str(entity_a.canonical_id), "dst_id": str(entity_b.canonical_id)},
        timeout_ms=3000,
    )
    results["entities_by_relation"] = await graph_store.traverse(
        "entities_by_relation", {"relation_type": "WORKS_WITH"}, timeout_ms=3000
    )
    results["co_mentioned"] = await graph_store.traverse(
        "co_mentioned", {"chunk_id": str(shared_chunk_id)}, timeout_ms=3000
    )
    results["top_entities_for_chunks"] = await graph_store.traverse(
        "top_entities_for_chunks", {"chunk_ids": [str(shared_chunk_id)]}, timeout_ms=3000
    )

    for name, paths in results.items():
        assert paths, f"{name} returned no paths against a graph that should satisfy it"


async def test_neighbors_respects_max_hops(
    graph_store: Neo4jGraphStore, graph_settings: Settings
) -> None:
    """A-B-C-D-E chain; max_hops=2 (config, unmodified) must never surface D or E."""
    chain = [make_entity(name=f"Chain {letter}", canonical_id=uuid4()) for letter in "ABCDE"]
    await graph_store.upsert_entities(chain)
    for src, dst in itertools.pairwise(chain):
        await graph_store.upsert_relations(
            [
                make_relation(
                    src_id=src.canonical_id,
                    dst_id=dst.canonical_id,
                    chunk_id=uuid4(),
                    doc_id="doc-chain",
                )
            ]
        )

    assert graph_settings.retrieval.graph.max_hops == 2

    paths = await graph_store.traverse(
        "neighbors", {"entity_id": str(chain[0].canonical_id)}, timeout_ms=3000
    )
    assert paths
    reached_ids = {node.canonical_id for path in paths for node in path.nodes}
    assert chain[3].canonical_id not in reached_ids  # D: 3 hops away
    assert chain[4].canonical_id not in reached_ids  # E: 4 hops away
    assert all(path.hops <= 2 for path in paths)


async def test_template_caps_degree(
    graph_store: Neo4jGraphStore, clean_neo4j: Any, graph_settings: Settings
) -> None:
    """A hub entity with 5,000 outgoing RELATES edges: `neighbors` must return at most
    `max_degree_per_hop` per hop and complete under `timeout_ms` -- proving the per-hop
    ORDER BY/LIMIT sits INSIDE the pattern, not as a trailing LIMIT after full expansion."""
    hub_id = uuid4()
    # canonical_id/chunk_id must be real UUID strings -- Neo4jGraphStore.traverse() parses every
    # node/relation it reads back via UUID(...), same as it would for production data.
    edges = [
        {"neighbor_id": str(uuid4()), "chunk_id": str(uuid4()), "confidence": i / 5000}
        for i in range(5000)
    ]
    await clean_neo4j.execute_query(
        """
        MERGE (hub:Entity {canonical_id: $hub_id})
        SET hub.name = 'Hub', hub.name_normalized = 'hub', hub.type = 'ORG', hub.mention_count = 1
        WITH hub
        UNWIND $edges AS row
        MERGE (n:Entity {canonical_id: row.neighbor_id})
        SET n.name = 'N' + row.neighbor_id, n.name_normalized = 'n' + row.neighbor_id,
            n.type = 'ORG', n.mention_count = 1
        MERGE (hub)-[r:RELATES {type: 'REL', chunk_id: row.chunk_id, doc_id: 'doc-hub'}]->(n)
        SET r.confidence = row.confidence, r.evidence_span = 'ev'
        """,
        {"hub_id": str(hub_id), "edges": edges},
        database_="neo4j",
    )

    timeout_ms = graph_settings.retrieval.graph.timeout_ms
    per_hop_cap = graph_settings.retrieval.graph.max_degree_per_hop
    start = time.perf_counter()
    paths = await graph_store.traverse(
        "neighbors", {"entity_id": str(hub_id)}, timeout_ms=timeout_ms
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    hop1_paths = [p for p in paths if p.hops == 1]
    assert 0 < len(hop1_paths) <= per_hop_cap
    assert elapsed_ms < timeout_ms, (
        f"neighbors traversal of a 5000-edge hub took {elapsed_ms:.1f}ms, "
        f"over the configured timeout_ms={timeout_ms}"
    )


async def test_extracted_relations_reach_graph(
    graph_store: Neo4jGraphStore, graph_settings: Settings
) -> None:
    """End-to-end: chunk a real corpus document, then run the REAL `extract_entities` and
    `resolve_entities` arq tasks (scripted `FakeLLMClient`, no real provider call -- this test is
    not `llm_quota`-marked) against a `Container` whose `graph_store` is this real
    `Neo4jGraphStore`. Confirms the relation the "LLM" extracted is queryable with provenance.
    The unit-level upsert tests only ever exercise hand-built `Relation` objects; this is what
    proves the BO-07 -> BO-08 handoff -- extraction's relation output surviving the queue hop
    (`ResolveEntitiesPayload.relations`) and landing via `GraphStore.upsert_relations` after
    resolution -- actually connects, through the real production code paths, not test-only
    helpers.
    """
    doc_id = "doc-corpus-04"
    text = _CORPUS_DOC.read_text(encoding="utf-8")
    chunks = _build_chunks(doc_id, "file:///corpus/04.txt", text, graph_settings)
    target_chunk = next(c for c in chunks if "Tim Cook" in c.text and "Apple" in c.text)

    fake_llm = FakeLLMClient()
    fake_llm.script_structured(
        "bulk",
        EntityExtraction(
            entities=[
                MentionOut(
                    chunk_id=str(target_chunk.chunk_id),
                    surface="Tim Cook",
                    type=EntityType.PERSON,
                    char_start=target_chunk.text.index("Tim Cook"),
                    char_end=target_chunk.text.index("Tim Cook") + len("Tim Cook"),
                    confidence=0.95,
                ),
                MentionOut(
                    chunk_id=str(target_chunk.chunk_id),
                    surface="Apple",
                    type=EntityType.ORG,
                    char_start=target_chunk.text.index("Apple"),
                    char_end=target_chunk.text.index("Apple") + len("Apple"),
                    confidence=0.97,
                ),
            ],
            relations=[
                RelationOut(
                    chunk_id=str(target_chunk.chunk_id),
                    src_surface="Tim Cook",
                    dst_surface="Apple",
                    type="CEO_OF",
                    confidence=0.9,
                    evidence_span="Tim Cook, Apple's CEO",
                )
            ],
        ),
    )

    fake_vector_store = FakeVectorStore()
    await fake_vector_store.upsert_chunks(
        chunks,
        [[0.0] * 8 for _ in chunks],
        [SparseVector(indices=[], values=[]) for _ in chunks],
    )
    metrics, _reader = make_metrics()
    container = Container(
        settings=graph_settings,
        ledger=FakeDocumentLedger(),
        sources=FakeSourceRegistry(),
        cache=FakeCache(),
        job_queue=FakeJobQueue(),
        readyz_prober=ReadyzProber({}, cache_s=5, timeout_s=1),
        metrics=metrics,
        vector_store=fake_vector_store,
        graph_store=graph_store,
        embedder=FakeEmbedder(dimensions=8),
        llm_client=fake_llm,
    )
    await container.ledger.register(doc_id, "file:///corpus/04.txt", "sha-corpus-04", "text/plain")
    await container.ledger.set_status(doc_id, DocumentStatus.EXTRACTING)

    ctx = {"container": container, "job_id": "job-e2e", "job_try": 1}
    extract_env = JobEnvelope(
        correlation_id="cid-e2e",
        otel={},
        enqueued_at=datetime.now(UTC),
        payload=ExtractEntitiesPayload(doc_id=doc_id, chunk_ids=[c.chunk_id for c in chunks]),
    )
    await extract_entities(ctx, extract_env)

    enqueued = [e for e in container.job_queue.enqueued if e["task"] == "resolve_entities"]
    assert len(enqueued) == 1
    resolve_env: JobEnvelope[ResolveEntitiesPayload] = enqueued[0]["envelope"]
    assert resolve_env.payload.relations, (
        "extract_entities must carry the LLM's relation output across the queue hop, not "
        "discard it the way BO-07 did"
    )

    await resolve_entities(ctx, resolve_env)

    paths = await graph_store.traverse(
        "entities_by_relation", {"relation_type": "CEO_OF"}, timeout_ms=3000
    )
    assert paths
    [path] = paths[:1]
    [relation] = path.relations
    assert relation.doc_id == doc_id
    assert relation.chunk_id == target_chunk.chunk_id
