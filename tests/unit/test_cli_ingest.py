"""`graphrag ingest` / `graphrag ingest --force` unit tests. See BLUEPRINT §7.3.

Exercises `_ingest_paths` against the all-fakes `container` fixture rather than through Typer's
runner: the behaviour under test is which documents get re-enqueued and what gets cleared first,
which is visible on the fakes and not in the CLI's output formatting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from graphrag.apps.api.main import Container
from graphrag.apps.cli.main import _ingest_paths
from graphrag.core.events import INGEST_DOCUMENT
from graphrag.core.models import DocumentStatus
from graphrag.services.ingestion.service import document_id, document_sha256

_CONTENT = b"Acme Corp. announced a new product today. It ships in March.\n"


@pytest.fixture
def corpus_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One real text file, with uploads redirected into tmp_path.

    `persist_upload` writes to a module-level relative path (`data/uploads`), so without this a
    unit test would scribble into the working tree.
    """
    import graphrag.apps._upload_storage as upload_storage

    monkeypatch.setattr(upload_storage, "UPLOAD_DIR_HOST", tmp_path / "uploads")
    path = tmp_path / "doc.txt"
    path.write_bytes(_CONTENT)
    return path


async def _register_at(container: Container, status: DocumentStatus) -> str:
    doc_id = document_id(_CONTENT)
    await container.ledger.register(
        doc_id, "file:///doc.txt", document_sha256(_CONTENT), "text/plain"
    )
    await container.ledger.set_status(doc_id, status)
    return doc_id


def _ingest_jobs(container: Container) -> list[dict]:
    return [e for e in container.job_queue.enqueued if e["task"] == INGEST_DOCUMENT]


@pytest.mark.parametrize("status", [DocumentStatus.FAILED, DocumentStatus.EXTRACTING])
async def test_plain_ingest_skips_a_broken_document(
    container: Container, corpus_file: Path, status: DocumentStatus
) -> None:
    """The trap `--force` exists for: re-running a folder does NOT repair a partial ingest.

    `register()` dedups on sha256 with no regard for status, so a FAILED document and one
    stranded mid-extraction are both skipped exactly like a healthy INDEXED one.
    """
    await _register_at(container, status)
    await _ingest_paths(container, [corpus_file], wait=False)
    assert _ingest_jobs(container) == []


async def test_force_repairs_a_failed_document(container: Container, corpus_file: Path) -> None:
    """--force clears BOTH suppressors — the ledger row and arq's retained job keys — then
    re-enqueues. Clearing only one of them leaves the re-ingest a silent no-op."""
    doc_id = await _register_at(container, DocumentStatus.FAILED)

    await _ingest_paths(container, [corpus_file], wait=False, force=True)

    assert container.job_queue.dropped == [doc_id]
    jobs = _ingest_jobs(container)
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == doc_id
    # Purged and re-registered, so the pipeline re-enters at the start of the state machine.
    record = await container.ledger.get(doc_id)
    assert record is not None
    assert record.status == DocumentStatus.PENDING
    assert record.error_code is None


async def test_force_repairs_a_document_stranded_mid_extraction(
    container: Container, corpus_file: Path
) -> None:
    """The job-timeout casualty. Nothing in the system detects this state on its own."""
    doc_id = await _register_at(container, DocumentStatus.EXTRACTING)
    await _ingest_paths(container, [corpus_file], wait=False, force=True)
    assert len(_ingest_jobs(container)) == 1
    assert container.job_queue.dropped == [doc_id]


async def test_force_refuses_an_indexed_document(container: Container, corpus_file: Path) -> None:
    """The guard that makes this safe to point at a corpus.

    --force re-pays full extraction quota for every document it touches, so a healthy document
    must not be re-ingested by a command aimed at a broken one. Nothing may be dropped either:
    clearing a healthy document's arq keys is itself a change.
    """
    await _register_at(container, DocumentStatus.INDEXED)

    await _ingest_paths(container, [corpus_file], wait=False, force=True)

    assert _ingest_jobs(container) == []
    assert container.job_queue.dropped == []


async def test_force_with_even_if_indexed_re_ingests(
    container: Container, corpus_file: Path
) -> None:
    """The explicit second flag, and only it, gets past the INDEXED guard."""
    doc_id = await _register_at(container, DocumentStatus.INDEXED)

    await _ingest_paths(container, [corpus_file], wait=False, force=True, even_if_indexed=True)

    assert len(_ingest_jobs(container)) == 1
    assert container.job_queue.dropped == [doc_id]


async def test_force_on_a_never_ingested_document_just_ingests(
    container: Container, corpus_file: Path
) -> None:
    """No ledger row to purge is not an error — --force on a fresh corpus is a plain ingest."""
    await _ingest_paths(container, [corpus_file], wait=False, force=True)
    assert len(_ingest_jobs(container)) == 1


async def test_first_ingest_enqueues(container: Container, corpus_file: Path) -> None:
    """Guards the tests above against being vacuous: without a pre-existing row, this path DOES
    enqueue, so a `[] == []` assertion elsewhere is meaningful."""
    await _ingest_paths(container, [corpus_file], wait=False)
    assert len(_ingest_jobs(container)) == 1
