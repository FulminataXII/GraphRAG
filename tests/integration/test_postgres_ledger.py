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


async def test_register_dedups_regardless_of_status(clean_pg: AsyncEngine) -> None:
    """The behaviour `--force` exists to work around, pinned so it is not rediscovered.

    `register()`'s ON CONFLICT is on `sha256` alone, so a document sitting at FAILED — or
    stranded mid-extraction — is skipped by a later `graphrag ingest` exactly like a healthy
    INDEXED one. Re-running a folder therefore never repairs a partial ingest.
    """
    ledger = PostgresDocumentLedger(clean_pg)
    await ledger.register("doc-broken", "file:///e.txt", "sha-broken", "text/plain")
    await ledger.set_status("doc-broken", DocumentStatus.FAILED, error_code="RATE_LIMITED")

    assert await ledger.register("doc-broken", "file:///e.txt", "sha-broken", "text/plain") is False


async def test_purge_lets_a_failed_document_be_registered_again(clean_pg: AsyncEngine) -> None:
    """`purge()` is the only thing that makes a broken document re-ingestable.

    Deliberately absent from the `DocumentLedger` port (BLUEPRINT §3.5 keeps rows so a re-upload
    of deleted content is recognisable); this proves the adapter-level escape hatch actually
    clears the sha256 conflict rather than merely deleting a row the unique index still knows
    about.
    """
    ledger = PostgresDocumentLedger(clean_pg)
    await ledger.register("doc-purge", "file:///f.txt", "sha-purge", "text/plain")
    await ledger.set_status("doc-purge", DocumentStatus.FAILED, error_code="JOB_TIMEOUT")

    assert await ledger.purge("doc-purge") is True
    assert await ledger.get("doc-purge") is None
    # The whole point: the same content may now be registered as a first-time ingest.
    assert await ledger.register("doc-purge", "file:///f.txt", "sha-purge", "text/plain") is True
    record = await ledger.get("doc-purge")
    assert record is not None
    assert record.status == DocumentStatus.PENDING
    assert record.error_code is None


async def test_purge_of_an_unknown_document_is_false_not_an_error(clean_pg: AsyncEngine) -> None:
    """`--force` on a never-ingested file must be a plain ingest, not a crash."""
    ledger = PostgresDocumentLedger(clean_pg)
    assert await ledger.purge("doc-never-existed") is False
