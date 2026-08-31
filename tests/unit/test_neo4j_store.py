"""`Neo4jGraphStore`/`CYPHER_TEMPLATES` unit tests (no real Neo4j). See BLUEPRINT §5.3 / BO-08.

Live-backend behavior (schema creation, actual traversal results, idempotence) is covered by
`tests/integration/test_neo4j_store.py`, which needs `make up`. What belongs here is everything
provable without a database: static Cypher hygiene, and validation that must happen BEFORE any
I/O — proven the same way `tests/unit/test_arq_queue.py` proves things about `ArqJobQueue`
without a real Redis, via a hand-rolled fake (BLUEPRINT §9: "NO mocks, NO MagicMock").
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from graphrag.adapters.neo4j_store import CYPHER_TEMPLATES, Neo4jGraphStore
from graphrag.core.errors import ValidationError
from tests.factories import make_relation

_NEO4J_STORE_PATH = Path(__file__).resolve().parents[2] / "graphrag" / "adapters" / "neo4j_store.py"


class _ExplodingDriver:
    """Any call proves the caller tried to touch Neo4j — which validation-before-I/O and
    unknown-template rejection must never do."""

    async def execute_query(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("execute_query should never be called")


def _store(settings: Any) -> Neo4jGraphStore:
    return Neo4jGraphStore(_ExplodingDriver(), settings)


def test_cypher_templates_keys_match_templates_enabled(settings: Any) -> None:
    """CYPHER_TEMPLATES keys must equal retrieval.graph.templates_enabled (BLUEPRINT §5.3) --
    a key in one and not the other is config/code drift, asserted rather than trusted."""
    assert set(CYPHER_TEMPLATES) == set(settings.retrieval.graph.templates_enabled)


def _cypher_string_assignments(tree: ast.Module) -> list[ast.expr]:
    """Every expression assigned to `CYPHER_TEMPLATES` (dict values), a module-level name ending
    in `_CYPHER`, or `_SCHEMA_STATEMENTS` (tuple elements) -- i.e. every place actual Cypher text
    lives in the module. Every one of these is a `Final`-annotated module constant, which the
    `ast` module parses as `AnnAssign` (a single target), not the multi-target `Assign` node a
    plain `x = ...` would produce.
    """
    exprs: list[ast.expr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or node.value is None:
            continue
        if not isinstance(node.target, ast.Name):
            continue
        name = node.target.id
        if name == "CYPHER_TEMPLATES" and isinstance(node.value, ast.Dict):
            exprs.extend(node.value.values)
        elif name.endswith("_CYPHER"):
            exprs.append(node.value)
        elif name == "_SCHEMA_STATEMENTS" and isinstance(node.value, ast.Tuple):
            exprs.extend(node.value.elts)
    return exprs


def test_no_fstring_cypher() -> None:
    """Static scan: every place Cypher text is defined in `neo4j_store.py` is a plain string
    literal -- no f-strings, `.format()`, or `+` concatenation. `traverse()`'s only defense
    against Cypher injection is that every query text is a fixed, reviewed constant; an f-string
    or concatenation would mean *some* fragment of the query is assembled at runtime, which is
    exactly what parameterization exists to avoid.
    """
    tree = ast.parse(_NEO4J_STORE_PATH.read_text(encoding="utf-8"))
    cypher_exprs = _cypher_string_assignments(tree)
    # Sanity: if this list is empty the scan below is vacuously true. 5 templates + the schema
    # statements + 9 named `_..._CYPHER` constants is the current shape; assert a lower bound
    # that only holds if the scan actually found real Cypher, not nothing.
    assert len(cypher_exprs) >= 14, (
        f"expected to find CYPHER_TEMPLATES / *_CYPHER / _SCHEMA_STATEMENTS entries to scan, "
        f"found {len(cypher_exprs)} -- either the module changed shape or this scan broke"
    )
    for expr in cypher_exprs:
        assert isinstance(expr, ast.Constant) and isinstance(expr.value, str), (
            f"non-literal Cypher text at neo4j_store.py:{expr.lineno} ({type(expr).__name__}) "
            "-- every query must be a plain string literal, never an f-string, .format(), or "
            "concatenation"
        )


async def test_traverse_rejects_unknown_template(settings: Any) -> None:
    store = _store(settings)
    with pytest.raises(ValidationError):
        await store.traverse("not_a_real_template", {}, timeout_ms=1000)


async def test_traverse_rejects_raw_cypher(settings: Any) -> None:
    """`traverse()` looks the `template` argument up BY KEY in `CYPHER_TEMPLATES`; it never
    executes its value as Cypher. Passing actual Cypher text as the key must be rejected exactly
    like any other unrecognized key -- proving there is no code path that falls back to treating
    an unmatched `template` as raw Cypher to run."""
    store = _store(settings)
    with pytest.raises(ValidationError):
        await store.traverse("MATCH (n) DETACH DELETE n", {}, timeout_ms=1000)


async def test_upsert_relations_rejects_null_provenance(settings: Any) -> None:
    """Raises before touching the database -- proven by `_ExplodingDriver`: if validation ran
    AFTER (or not at all) and the code tried to execute a query, this test would fail with
    `_ExplodingDriver`'s AssertionError instead of the expected ValidationError."""
    store = _store(settings)
    missing_chunk_id = make_relation().model_copy(update={"chunk_id": None})
    with pytest.raises(ValidationError):
        await store.upsert_relations([missing_chunk_id])

    missing_doc_id = make_relation().model_copy(update={"doc_id": None})
    with pytest.raises(ValidationError):
        await store.upsert_relations([missing_doc_id])


async def test_upsert_relations_rejects_whole_batch_on_one_bad_relation(settings: Any) -> None:
    """A single invalid relation in a batch rejects the WHOLE call -- filter-and-continue would
    silently drop provenance-less data instead of surfacing it."""
    store = _store(settings)
    good = make_relation()
    bad = make_relation().model_copy(update={"chunk_id": None})
    with pytest.raises(ValidationError):
        await store.upsert_relations([good, bad])
