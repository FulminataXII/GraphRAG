"""`PostgresDocumentLedger` against a real Postgres. See BLUEPRINT §5.4 / BUILD_ORDER BO-03.

Marked `integration`: `register()`'s ON CONFLICT dedup and `bump_corpus_version()`'s atomic
UPDATE...RETURNING are real-database behaviors that a hand-rolled fake SQL engine can't
meaningfully stand in for — BUILD_ORDER.md doesn't tag every one of these with `[integration]`,
but the `unit: no I/O` marker (BLUEPRINT §9) rules out testing a Postgres adapter without one.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from graphrag.adapters.postgres.ledger import PostgresDocumentLedger
from graphrag.core.errors import ConflictError
from graphrag.core.models import DocumentStatus

pytestmark = pytest.mark.integration


async def test_ledger_register_dedups_on_sha256(clean_pg: AsyncEngine) -> None:
    ledger = PostgresDocumentLedger(clean_pg)
    first = await ledger.register("doc-a", "file:///a.txt", "sha-dup", "text/plain")
    second = await ledger.register("doc-b", "file:///b.txt", "sha-dup", "text/plain")
    assert first is True
    assert second is False


async def test_ledger_illegal_transition_raises(clean_pg: AsyncEngine) -> None:
    ledger = PostgresDocumentLedger(clean_pg)
    await ledger.register("doc-illegal", "file:///c.txt", "sha-illegal", "text/plain")
    for status in (
        DocumentStatus.PARSING,
        DocumentStatus.EMBEDDING,
        DocumentStatus.EXTRACTING,
        DocumentStatus.RESOLVING,
        DocumentStatus.INDEXED,
    ):
        await ledger.set_status("doc-illegal", status)

    with pytest.raises(ConflictError):
        await ledger.set_status("doc-illegal", DocumentStatus.PARSING)


async def test_ledger_get_roundtrips(clean_pg: AsyncEngine) -> None:
    ledger = PostgresDocumentLedger(clean_pg)
    await ledger.register("doc-get", "file:///d.txt", "sha-get", "text/markdown")
    record = await ledger.get("doc-get")
    assert record is not None
    assert record.doc_id == "doc-get"
    assert record.status == DocumentStatus.PENDING
    assert record.sha256 == "sha-get"
    assert record.created_at.tzinfo is not None


async def test_ledger_get_unknown_returns_none(clean_pg: AsyncEngine) -> None:
    ledger = PostgresDocumentLedger(clean_pg)
    assert await ledger.get("does-not-exist") is None


async def test_corpus_version_monotonic_under_concurrency(clean_pg: AsyncEngine) -> None:
    ledger = PostgresDocumentLedger(clean_pg)
    assert await ledger.current_corpus_version() == 0

    results = await asyncio.gather(*[ledger.bump_corpus_version() for _ in range(20)])

    assert sorted(results) == list(range(1, 21))  # no lost updates, no duplicates
    assert await ledger.current_corpus_version() == 20
