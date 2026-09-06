"""PostgresDocumentLedger — implements `core.ports.DocumentLedger`. See BLUEPRINT §5.4.

The one row per `doc_id` here IS the document state machine: PENDING -> PARSING -> EMBEDDING ->
EXTRACTING -> RESOLVING -> INDEXED, with FAILED and DELETING reachable from most states. Illegal
transitions (e.g. INDEXED -> PARSING) raise `ConflictError` rather than silently overwriting.

`corpus_version` is tracked in a singleton counter row (`corpus_version_counter`, id=1) rather
than derived from `documents` (no single column there represents "the corpus", and MAX(corpus_
version) would race under concurrent bumps without an explicit atomic UPDATE ... RETURNING).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from graphrag.adapters.postgres.tables import corpus_version_counter, documents
from graphrag.core.errors import ConflictError
from graphrag.core.models import DocumentRecord, DocumentStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

# Legal forward transitions. FAILED and DELETING are reachable from any non-terminal state;
# terminal states (INDEXED, FAILED) may still move to DELETING (a document can be deleted after
# successful indexing, or after landing in a permanent failure).
_TERMINAL: frozenset[DocumentStatus] = frozenset({DocumentStatus.DELETING})
_FORWARD: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    DocumentStatus.PENDING: frozenset(
        {DocumentStatus.PARSING, DocumentStatus.FAILED, DocumentStatus.DELETING}
    ),
    DocumentStatus.PARSING: frozenset(
        {DocumentStatus.EMBEDDING, DocumentStatus.FAILED, DocumentStatus.DELETING}
    ),
    DocumentStatus.EMBEDDING: frozenset(
        {DocumentStatus.EXTRACTING, DocumentStatus.FAILED, DocumentStatus.DELETING}
    ),
    DocumentStatus.EXTRACTING: frozenset(
        {DocumentStatus.RESOLVING, DocumentStatus.FAILED, DocumentStatus.DELETING}
    ),
    DocumentStatus.RESOLVING: frozenset(
        {DocumentStatus.INDEXED, DocumentStatus.FAILED, DocumentStatus.DELETING}
    ),
    DocumentStatus.INDEXED: frozenset({DocumentStatus.DELETING}),
    DocumentStatus.FAILED: frozenset(
        {DocumentStatus.PENDING, DocumentStatus.DELETING}
    ),  # retry re-enters at PENDING
    DocumentStatus.DELETING: frozenset(),
}


def _row_to_record(row: object) -> DocumentRecord:
    return DocumentRecord(
        doc_id=row.doc_id,  # type: ignore[attr-defined]
        uri=row.uri,  # type: ignore[attr-defined]
        sha256=row.sha256,  # type: ignore[attr-defined]
        mime_type=row.mime_type,  # type: ignore[attr-defined]
        status=DocumentStatus(row.status),  # type: ignore[attr-defined]
        error_code=row.error_code,  # type: ignore[attr-defined]
        attempts=row.attempts,  # type: ignore[attr-defined]
        corpus_version=row.corpus_version,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
        updated_at=row.updated_at,  # type: ignore[attr-defined]
    )


class PostgresDocumentLedger:
    """Implements `DocumentLedger`.

    Contract (BLUEPRINT §5.4):
        - register() uses INSERT ... ON CONFLICT (sha256) DO NOTHING; returns rowcount == 1.
        - set_status() validates the transition against the DocumentStatus state machine and
          raises ConflictError on an illegal move.
        - bump_corpus_version() is a single atomic UPDATE ... RETURNING.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def register(self, doc_id: str, uri: str, sha256: str, mime_type: str) -> bool:
        now = datetime.now(UTC)
        stmt = (
            pg_insert(documents)
            .values(
                doc_id=doc_id,
                uri=uri,
                sha256=sha256,
                mime_type=mime_type,
                status=DocumentStatus.PENDING.value,
                error_code=None,
                attempts=0,
                corpus_version=0,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["sha256"])
        )
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return result.rowcount == 1

    async def set_status(
        self, doc_id: str, status: DocumentStatus, error_code: str | None = None
    ) -> None:
        async with self._engine.begin() as conn:
            current_row = (
                await conn.execute(select(documents.c.status).where(documents.c.doc_id == doc_id))
            ).first()
            if current_row is None:
                raise ConflictError(
                    f"cannot set status on unknown document {doc_id!r}",
                    details={"doc_id": doc_id},
                )
            current = DocumentStatus(current_row.status)
            if status != current and status not in _FORWARD.get(current, frozenset()):
                raise ConflictError(
                    f"illegal document status transition: {current.value} -> {status.value}",
                    details={"doc_id": doc_id, "from": current.value, "to": status.value},
                )
            await conn.execute(
                update(documents)
                .where(documents.c.doc_id == doc_id)
                .values(
                    status=status.value,
                    error_code=error_code,
                    updated_at=datetime.now(UTC),
                )
            )

    async def purge(self, doc_id: str) -> bool:
        """Delete one document's ledger row outright. Returns True if a row was removed.

        Deliberately NOT on the `core.ports.DocumentLedger` Protocol (BLUEPRINT §3.5), which
        specifies no row-removal method on purpose: a retained row is what makes a re-upload of
        deleted content look like the re-upload it is rather than a first-time ingest. That
        design has one consequence it did not intend — `register()` dedups on `sha256` with no
        regard for status, so a document that FAILED or was cancelled mid-extraction is skipped
        by a later `graphrag ingest` exactly like a healthy INDEXED one, and no amount of
        re-running the folder repairs it.

        This is the escape hatch for that, reachable only from `graphrag ingest --force`. It
        does NOT touch `chunk_sources`: those rows are content-addressed and re-inserted with
        ON CONFLICT DO NOTHING, so leaving them is what makes the forced re-ingest skip
        re-embedding work it has already paid for. There is no foreign key from `chunk_sources`
        to `documents`, so this cascades nowhere.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(delete(documents).where(documents.c.doc_id == doc_id))
            return result.rowcount == 1

    async def get(self, doc_id: str) -> DocumentRecord | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(select(documents).where(documents.c.doc_id == doc_id))
            ).first()
            return _row_to_record(row) if row is not None else None

    async def bump_corpus_version(self) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(corpus_version_counter)
                .where(corpus_version_counter.c.id == 1)
                .values(value=corpus_version_counter.c.value + 1)
                .returning(corpus_version_counter.c.value)
            )
            row = result.first()
            assert row is not None  # seeded by migration; id=1 always exists
            return int(row.value)

    async def current_corpus_version(self) -> int:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(corpus_version_counter.c.value).where(corpus_version_counter.c.id == 1)
            )
            row = result.first()
            return int(row.value) if row is not None else 0
