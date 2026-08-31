"""Neo4jGraphStore — implements `core.ports.GraphStore`. See BLUEPRINT §5.3.

Schema (ARCHITECTURE §6.3, Community-Edition compatible — property uniqueness constraints and
range indexes only, no existence/key/type constraints, which are Enterprise-only):

    (:Document {doc_id, uri, title, sha256})
    (:Chunk    {chunk_id, text})                            -- FULL text, never a preview
    (:Entity   {canonical_id, name, name_normalized, type, mention_count, created_at})
    (Document)-[:HAS_CHUNK {ord, page, char_start, char_end, ingested_at}]->(Chunk)
    (Entity)-[:ALIAS_OF {score, method, decided_at}]->(Entity)     -- loser -> canonical, never deleted
    (Entity)-[:RELATES {type, confidence, chunk_id, doc_id, evidence_span}]->(Entity)

SPEC GAP — `HAS_CHUNK` offsets: ARCHITECTURE §6.3 declares `HAS_CHUNK {ord}` (ord only), but
BLUEPRINT §3.5's `get_chunks` contract requires reconstructing `SourceRef`s (doc_id, uri, page,
char_start, char_end, ingested_at) "from the (:Document)-[:HAS_CHUNK]->(:Chunk) edges, which is
why HAS_CHUNK carries the offsets" — i.e. BLUEPRINT itself says HAS_CHUNK carries more than
`ord`. BLUEPRINT wins on disagreement (per its own precedence rule), so HAS_CHUNK here also
carries `page`, `char_start`, `char_end`, `ingested_at`; `uri` is read off the Document node
instead of duplicated onto every edge. Reported alongside the rest of this BO's findings.

SPEC GAP — no MENTIONS-writing port method: ARCHITECTURE §6.3 also declares
`(Chunk)-[:MENTIONS {surface, confidence, char_start, char_end}]->(Entity)`, but
`core.ports.GraphStore` (BLUEPRINT §3.5, authoritative) exposes no method to write it — only
`upsert_entities`/`upsert_relations`/`add_alias`/document+chunk methods. Inventing a
`upsert_mentions` port method would mean editing `core/ports.py`, a BO-01 file this BO doesn't
own, so MENTIONS edges are never created. `co_mentioned` and `top_entities_for_chunks` are
implemented purely over `RELATES.chunk_id` instead (every relation already carries chunk_id/
doc_id per ARCHITECTURE's own "every relationship carries chunk_id + doc_id" design principle),
which needs no MENTIONS edge. Reported as a judgment call, not silently assumed.

JUDGMENT CALL — `Entity.aliases` on `traverse()` results: the domain model requires
`aliases: list[str]`, but the graph stores aliases only as incoming `ALIAS_OF` edges, not a node
property, so reconstructing them for every entity node touched by a traversal would mean an
extra subquery per node on every template. None of this BO's tests (or BLUEPRINT's `traverse`
contract) require alias data on path results, so entities reconstructed by `traverse()` carry
`aliases=[]`. `entities`/`get_chunks` results are unaffected — `upsert_entities` is always called
with the real `Entity.aliases` from `ResolutionResult`, so that data is durably stored via
`ALIAS_OF` edges even though `traverse()` doesn't read it back.

JUDGMENT CALL — `GraphPath.score`: `core/models.py` declares this field but neither BLUEPRINT nor
ARCHITECTURE documents what it should mean (found only by a live-integration test failing with
"field required" — grep for it before trusting any earlier reading of this file's neighboring
`Relation`/`Entity` classes). Every `RELATES`-bearing path here scores as the mean confidence of
its relations (`_path_score`) — a natural, defensible choice given every template's own Cypher
already ranks candidates by `r.confidence`. The zero-hop `top_entities_for_chunks` path (no
relation to average) scores as the ranked `degree` value itself, which is what that template's
own `ORDER BY` ranks by.

Per-hop degree cap (BLUEPRINT §5.3): applies to `neighbors`, the one template that fans out from
a single node — `ORDER BY r.confidence DESC LIMIT $per_hop_cap` sits INSIDE a `CALL (...) { }`
subquery per hop (Cypher's documented "top-N per group" idiom: a correlated subquery executes
once per incoming row), so a hub's second hop only ever expands from the top `per_hop_cap`
survivors of the first, never the full fan-out. `path_between` uses Neo4j's native
`shortestPath()`, which is a bounded bidirectional search, not enumeration, so it doesn't carry
the same hub-blowup risk. `entities_by_relation` is a single-hop filter, so a plain
`ORDER BY ... LIMIT $max_paths` is applied directly (no compounding to guard against).
`per_hop_cap`/`max_paths` are always ADAPTER-injected from `retrieval.graph` config in
`traverse()`, overriding any same-named key a caller passes — a safety bound the caller must not
be able to loosen.

`max_hops` (`retrieval.graph.max_hops`, currently 2) is baked into `neighbors`' Cypher text as an
explicit two-stage unroll, not a variable-length `*1..$max_hops` pattern: Cypher's per-hop
ORDER BY/LIMIT idiom has no equivalent for a *dynamic* hop count, and `CYPHER_TEMPLATES` is
declared `Final` (a static dict, not a function of settings). If `retrieval.graph.max_hops` is
ever reconfigured away from 2, this template's Cypher text would need to change with it — a
coupling neither BLUEPRINT nor ARCHITECTURE resolves, reported as a spec gap rather than solved
by inventing a dynamic-hop-count query builder this BO wasn't asked for.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from neo4j import Query, RoutingControl
from neo4j.exceptions import DriverError, Neo4jError

from graphrag.core.errors import GraphBackendUnavailable, ValidationError
from graphrag.core.ids import content_hash
from graphrag.core.models import Chunk, Entity, EntityType, GraphPath, Relation, SourceRef

if TYPE_CHECKING:
    from neo4j import AsyncDriver
    from neo4j.graph import Node, Path, Relationship

    from graphrag.config.settings import Settings

CYPHER_TEMPLATES: Final[dict[str, str]] = {
    "neighbors": """
        MATCH (start:Entity {canonical_id: $entity_id})
        CALL (start) {
          MATCH (start)-[r1:RELATES]-(n1:Entity)
          RETURN r1, n1
          ORDER BY r1.confidence DESC
          LIMIT $per_hop_cap
        }
        WITH start, r1, n1
        CALL (start, n1) {
          OPTIONAL MATCH (n1)-[r2:RELATES]-(n2:Entity)
          WHERE n2.canonical_id <> start.canonical_id
          RETURN r2, n2
          ORDER BY r2.confidence DESC
          LIMIT $per_hop_cap
        }
        RETURN start, r1, n1, r2, n2
        LIMIT $max_paths
    """,
    "path_between": """
        MATCH (a:Entity {canonical_id: $src_id}), (b:Entity {canonical_id: $dst_id})
        MATCH p = shortestPath((a)-[:RELATES*1..2]-(b))
        RETURN p
        LIMIT $max_paths
    """,
    "entities_by_relation": """
        MATCH (s:Entity)-[r:RELATES {type: $relation_type}]->(d:Entity)
        RETURN s, r, d
        ORDER BY r.confidence DESC
        LIMIT $max_paths
    """,
    "co_mentioned": """
        MATCH (a:Entity)-[r:RELATES {chunk_id: $chunk_id}]-(b:Entity)
        WHERE a.canonical_id < b.canonical_id
        RETURN a, r, b
        ORDER BY r.confidence DESC
        LIMIT $max_paths
    """,
    "top_entities_for_chunks": """
        UNWIND $chunk_ids AS cid
        MATCH (a:Entity)-[r:RELATES {chunk_id: cid}]-(b:Entity)
        WITH a, count(DISTINCT r) AS degree
        ORDER BY degree DESC
        LIMIT $max_paths
        RETURN a, degree
    """,
}

_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    "CREATE CONSTRAINT doc_id IF NOT EXISTS FOR (d:Document) REQUIRE d.doc_id IS UNIQUE",
    "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE",
    "CREATE CONSTRAINT entity_cid IF NOT EXISTS FOR (e:Entity) REQUIRE e.canonical_id IS UNIQUE",
    "CREATE INDEX entity_norm IF NOT EXISTS FOR (e:Entity) ON (e.name_normalized)",
)

_UPSERT_DOCUMENT_CYPHER: Final[str] = """
    MERGE (d:Document {doc_id: $doc_id})
    SET d.uri = $uri, d.title = $title, d.sha256 = $sha256
"""

_UPSERT_CHUNKS_CYPHER: Final[str] = """
    MATCH (d:Document {doc_id: $doc_id})
    UNWIND $chunks AS row
    MERGE (c:Chunk {chunk_id: row.chunk_id})
    SET c.text = row.text
    MERGE (d)-[h:HAS_CHUNK]->(c)
    SET h.ord = row.ord, h.page = row.page, h.char_start = row.char_start,
        h.char_end = row.char_end, h.ingested_at = row.ingested_at
"""

_GET_CHUNKS_CYPHER: Final[str] = """
    MATCH (d:Document)-[h:HAS_CHUNK]->(c:Chunk)
    WHERE c.chunk_id IN $chunk_ids
    RETURN c.chunk_id AS chunk_id, c.text AS text, d.doc_id AS doc_id, d.uri AS uri,
           h.page AS page, h.char_start AS char_start, h.char_end AS char_end,
           h.ingested_at AS ingested_at
"""

_UPSERT_ENTITIES_CYPHER: Final[str] = """
    UNWIND $entities AS row
    MERGE (e:Entity {canonical_id: row.canonical_id})
    ON CREATE SET e.created_at = datetime()
    SET e.name = row.name, e.name_normalized = row.name_normalized,
        e.type = row.type, e.mention_count = row.mention_count
"""

_UPSERT_RELATIONS_CYPHER: Final[str] = """
    UNWIND $relations AS row
    MERGE (s:Entity {canonical_id: row.src_id})
    MERGE (d:Entity {canonical_id: row.dst_id})
    MERGE (s)-[r:RELATES {type: row.type, chunk_id: row.chunk_id, doc_id: row.doc_id}]->(d)
    SET r.confidence = row.confidence, r.evidence_span = row.evidence_span
"""

_ADD_ALIAS_CYPHER: Final[str] = """
    MERGE (a:Entity {canonical_id: $alias_id})
    MERGE (c:Entity {canonical_id: $canonical_id})
    MERGE (a)-[r:ALIAS_OF]->(c)
    ON CREATE SET r.decided_at = datetime()
    SET r.score = $score, r.method = $method
"""

_DELETE_CHUNKS_CYPHER: Final[str] = """
    MATCH (c:Chunk) WHERE c.chunk_id IN $chunk_ids
    DETACH DELETE c
"""

_DELETE_DOCUMENT_ORPHANED_CHUNKS_CYPHER: Final[str] = """
    MATCH (d:Document {doc_id: $doc_id})-[:HAS_CHUNK]->(c:Chunk)
    WHERE NOT EXISTS {
      MATCH (other:Document)-[:HAS_CHUNK]->(c)
      WHERE other.doc_id <> $doc_id
    }
    DETACH DELETE c
"""

_DELETE_DOCUMENT_CYPHER: Final[str] = """
    MATCH (d:Document {doc_id: $doc_id})
    DETACH DELETE d
"""

_HEALTH_CYPHER: Final[str] = "RETURN 1"


def _entity_from_node(node: Node) -> Entity:
    return Entity(
        canonical_id=UUID(node["canonical_id"]),
        name=node["name"],
        name_normalized=node["name_normalized"],
        type=EntityType(node["type"]),
        aliases=[],
        mention_count=node["mention_count"],
    )


def _relation_from_rel(rel: Relationship) -> Relation:
    return Relation(
        src_id=UUID(rel.start_node["canonical_id"]),
        dst_id=UUID(rel.end_node["canonical_id"]),
        type=rel["type"],
        confidence=rel["confidence"],
        chunk_id=UUID(rel["chunk_id"]),
        doc_id=rel["doc_id"],
        evidence_span=rel["evidence_span"],
    )


def _path_score(relations: Sequence[Relation]) -> float:
    """`GraphPath.score` has no BLUEPRINT-documented meaning beyond its type; mean relation
    confidence along the path is the natural, defensible choice given every relation already
    carries one and every template's own Cypher ranks candidates by `r.confidence`."""
    if not relations:
        return 0.0
    return sum(relation.confidence for relation in relations) / len(relations)


def _path_from_neo4j_path(path: Path) -> GraphPath:
    relations = [_relation_from_rel(rel) for rel in path.relationships]
    return GraphPath(
        nodes=[_entity_from_node(node) for node in path.nodes],
        relations=relations,
        chunk_ids=sorted({rel.chunk_id for rel in relations}, key=str),
        hops=len(relations),
        score=_path_score(relations),
    )


class Neo4jGraphStore:
    """Implements `GraphStore` (BLUEPRINT §5.3 / §3.5).

    Contract:
        - ensure_schema() creates constraints/indexes idempotently (IF NOT EXISTS).
        - upsert_* use MERGE keyed on the id property; re-running over the same corpus creates
          zero new nodes and zero new relationships.
        - upsert_relations REJECTS (raises ValidationError) any Relation with a null chunk_id
          or doc_id before touching the database.
        - upsert_chunks stores the FULL Chunk.text.
        - traverse(template, params) looks template up in CYPHER_TEMPLATES by key and raises
          ValidationError on an unknown key. It never accepts raw Cypher.
        - Library exceptions wrap to GraphBackendUnavailable.
    """

    def __init__(self, driver: AsyncDriver, settings: Settings) -> None:
        self._driver = driver
        self._database = settings.stores.neo4j.database
        self._graph = settings.retrieval.graph

    async def _execute(
        self,
        text: str,
        params: dict[str, Any],
        *,
        routing: RoutingControl,
        timeout_s: float | None = None,
    ) -> list[Any]:
        query: Query | str = Query(text, timeout=timeout_s) if timeout_s is not None else text
        try:
            records, _summary, _keys = await self._driver.execute_query(
                query, params, routing_=routing, database_=self._database
            )
        except (Neo4jError, DriverError) as exc:
            raise GraphBackendUnavailable(f"Neo4j operation failed: {exc}") from exc
        return records

    async def ensure_schema(self) -> None:
        for statement in _SCHEMA_STATEMENTS:
            await self._execute(statement, {}, routing=RoutingControl.WRITE)

    async def upsert_document(self, doc_id: str, uri: str, title: str, sha256: str) -> None:
        await self._execute(
            _UPSERT_DOCUMENT_CYPHER,
            {"doc_id": doc_id, "uri": uri, "title": title, "sha256": sha256},
            routing=RoutingControl.WRITE,
        )

    async def upsert_chunks(self, doc_id: str, chunks: Sequence[Chunk]) -> None:
        if not chunks:
            return
        rows = []
        for ord_, chunk in enumerate(chunks):
            source = next((s for s in chunk.sources if s.doc_id == doc_id), None)
            if source is None:
                raise ValidationError(
                    f"Chunk {chunk.chunk_id} has no SourceRef for doc_id={doc_id!r}",
                    details={"chunk_id": str(chunk.chunk_id), "doc_id": doc_id},
                )
            rows.append(
                {
                    "chunk_id": str(chunk.chunk_id),
                    "text": chunk.text,
                    "ord": ord_,
                    "page": source.page,
                    "char_start": source.char_start,
                    "char_end": source.char_end,
                    "ingested_at": source.ingested_at,
                }
            )
        await self._execute(
            _UPSERT_CHUNKS_CYPHER,
            {"doc_id": doc_id, "chunks": rows},
            routing=RoutingControl.WRITE,
        )

    async def upsert_entities(self, entities: Sequence[Entity]) -> None:
        if not entities:
            return
        rows = [
            {
                "canonical_id": str(entity.canonical_id),
                "name": entity.name,
                "name_normalized": entity.name_normalized,
                "type": entity.type.value,
                "mention_count": entity.mention_count,
            }
            for entity in entities
        ]
        await self._execute(
            _UPSERT_ENTITIES_CYPHER, {"entities": rows}, routing=RoutingControl.WRITE
        )

    async def upsert_relations(self, relations: Sequence[Relation]) -> None:
        for relation in relations:
            if relation.chunk_id is None or relation.doc_id is None:
                raise ValidationError(
                    "Relation is missing required provenance (chunk_id/doc_id)",
                    details={"src_id": str(relation.src_id), "dst_id": str(relation.dst_id)},
                )
        if not relations:
            return
        rows = [
            {
                "src_id": str(relation.src_id),
                "dst_id": str(relation.dst_id),
                "type": relation.type,
                "chunk_id": str(relation.chunk_id),
                "doc_id": relation.doc_id,
                "confidence": relation.confidence,
                "evidence_span": relation.evidence_span,
            }
            for relation in relations
        ]
        await self._execute(
            _UPSERT_RELATIONS_CYPHER, {"relations": rows}, routing=RoutingControl.WRITE
        )

    async def add_alias(
        self, alias_id: UUID, canonical_id: UUID, score: float, method: str
    ) -> None:
        await self._execute(
            _ADD_ALIAS_CYPHER,
            {
                "alias_id": str(alias_id),
                "canonical_id": str(canonical_id),
                "score": score,
                "method": method,
            },
            routing=RoutingControl.WRITE,
        )

    async def traverse(
        self, template: str, params: dict[str, Any], *, timeout_ms: int
    ) -> list[GraphPath]:
        cypher = CYPHER_TEMPLATES.get(template)
        if cypher is None:
            raise ValidationError(
                f"Unknown graph traversal template: {template!r}",
                details={"template": template, "known": sorted(CYPHER_TEMPLATES)},
            )
        full_params = {
            **params,
            "per_hop_cap": self._graph.max_degree_per_hop,
            "max_paths": self._graph.max_paths,
        }
        records = await self._execute(
            cypher,
            full_params,
            routing=RoutingControl.READ,
            timeout_s=timeout_ms / 1000,
        )
        if template == "neighbors":
            return self._paths_from_neighbors(records)
        if template == "path_between":
            return [_path_from_neo4j_path(record["p"]) for record in records]
        if template == "entities_by_relation":
            return [self._one_hop_path(record["s"], record["r"], record["d"]) for record in records]
        if template == "co_mentioned":
            return [self._one_hop_path(record["a"], record["r"], record["b"]) for record in records]
        # top_entities_for_chunks: no traversal, one degenerate zero-hop path per ranked entity.
        # score is the ranking degree itself -- there is no relation to average a confidence
        # from, and `degree` is already what the template's own ORDER BY ranks candidates by.
        return [
            GraphPath(
                nodes=[_entity_from_node(record["a"])],
                relations=[],
                chunk_ids=[],
                hops=0,
                score=float(record["degree"]),
            )
            for record in records
        ]

    def _one_hop_path(self, start: Node, rel: Relationship, end: Node) -> GraphPath:
        relation = _relation_from_rel(rel)
        return GraphPath(
            nodes=[_entity_from_node(start), _entity_from_node(end)],
            relations=[relation],
            chunk_ids=[relation.chunk_id],
            hops=1,
            score=_path_score([relation]),
        )

    def _paths_from_neighbors(self, records: list[Any]) -> list[GraphPath]:
        paths: list[GraphPath] = []
        seen_hop1: set[str] = set()
        for record in records:
            start, r1, n1, r2, n2 = (
                record["start"],
                record["r1"],
                record["n1"],
                record["r2"],
                record["n2"],
            )
            rel1 = _relation_from_rel(r1)
            hop1_key = f"{start['canonical_id']}->{n1['canonical_id']}->{rel1.chunk_id}"
            if hop1_key not in seen_hop1:
                seen_hop1.add(hop1_key)
                paths.append(
                    GraphPath(
                        nodes=[_entity_from_node(start), _entity_from_node(n1)],
                        relations=[rel1],
                        chunk_ids=[rel1.chunk_id],
                        hops=1,
                        score=_path_score([rel1]),
                    )
                )
            if r2 is not None and n2 is not None:
                rel2 = _relation_from_rel(r2)
                paths.append(
                    GraphPath(
                        nodes=[
                            _entity_from_node(start),
                            _entity_from_node(n1),
                            _entity_from_node(n2),
                        ],
                        relations=[rel1, rel2],
                        chunk_ids=sorted({rel1.chunk_id, rel2.chunk_id}, key=str),
                        hops=2,
                        score=_path_score([rel1, rel2]),
                    )
                )
        return paths

    async def get_chunks(self, chunk_ids: Sequence[UUID]) -> list[Chunk]:
        if not chunk_ids:
            return []
        records = await self._execute(
            _GET_CHUNKS_CYPHER,
            {"chunk_ids": [str(cid) for cid in chunk_ids]},
            routing=RoutingControl.READ,
        )
        by_chunk: dict[UUID, dict[str, Any]] = {}
        for record in records:
            cid = UUID(record["chunk_id"])
            entry = by_chunk.setdefault(cid, {"text": record["text"], "sources": []})
            ingested_at = record["ingested_at"]
            entry["sources"].append(
                SourceRef(
                    doc_id=record["doc_id"],
                    uri=record["uri"],
                    page=record["page"],
                    char_start=record["char_start"],
                    char_end=record["char_end"],
                    ingested_at=(
                        ingested_at.to_native()
                        if hasattr(ingested_at, "to_native")
                        else ingested_at
                    ),
                )
            )
        return [
            Chunk(
                chunk_id=cid,
                text=entry["text"],
                content_hash=content_hash(entry["text"]),
                sources=entry["sources"],
                entity_ids=[],
            )
            for cid, entry in by_chunk.items()
        ]

    async def delete_chunks(self, chunk_ids: Sequence[UUID]) -> None:
        if not chunk_ids:
            return
        await self._execute(
            _DELETE_CHUNKS_CYPHER,
            {"chunk_ids": [str(cid) for cid in chunk_ids]},
            routing=RoutingControl.WRITE,
        )

    async def delete_document(self, doc_id: str) -> None:
        await self._execute(
            _DELETE_DOCUMENT_ORPHANED_CHUNKS_CYPHER,
            {"doc_id": doc_id},
            routing=RoutingControl.WRITE,
        )
        await self._execute(
            _DELETE_DOCUMENT_CYPHER, {"doc_id": doc_id}, routing=RoutingControl.WRITE
        )

    async def health(self) -> bool:
        try:
            await self._execute(_HEALTH_CYPHER, {}, routing=RoutingControl.READ)
        except GraphBackendUnavailable:
            return False
        return True


__all__ = ["CYPHER_TEMPLATES", "Neo4jGraphStore"]
