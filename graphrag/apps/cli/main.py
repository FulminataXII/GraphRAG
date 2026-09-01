"""Typer CLI. See BLUEPRINT §7.3.

BO-02 built `trail`. This BO adds `ingest`, `seed`, `query --show-chunk-ids`, and `reindex`
(exactly BUILD_ORDER.md's BO-05 list). `eval` and `config-hash` land in later build orders —
adding stubs for them now would be building ahead of the current BO.

`query` is a direct hybrid-search debug tool, not the full self-correcting orchestration
pipeline (`POST /v1/query`) — that depends on `services/retrieval` and `services/orchestration`,
which don't exist until BO-09/10. It exists now because MANUAL M-5 needs it before BO-11:
`--show-chunk-ids` is how `gold_chunk_ids` get collected without anyone transcribing a UUID off
a screen.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import magic
import typer

from graphrag.adapters.telemetry.trail import TrailBuilder
from graphrag.apps._upload_storage import persist_upload
from graphrag.apps.api.main import Container
from graphrag.config.settings import get_settings
from graphrag.core.events import IngestDocumentPayload, JobEnvelope
from graphrag.core.ids import chunk_id, new_correlation_id
from graphrag.core.models import DocumentStatus
from graphrag.services.ingestion.chunker import chunk_document
from graphrag.services.ingestion.parser import DocumentParser
from graphrag.services.ingestion.service import ProjectionService, document_id, document_sha256

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.callback()
def _callback() -> None:
    """graphrag — see `--help` on a subcommand for details.

    An empty callback, not a no-op: Typer collapses a single `@app.command()` into a
    top-level command with NO subcommand name (so `graphrag <cid>` would work but
    `graphrag trail <cid>` — what the Makefile and BLUEPRINT §7.3 both specify, and what
    BO-05/BO-11 need this to keep meaning once they add more commands here — would not).
    Registering any callback forces Typer's multi-command mode regardless of command count.
    """


@app.command()
def trail(
    correlation_id: Annotated[
        str, typer.Argument(help="Correlation ID to build a debug bundle for.")
    ],
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Output path. Defaults to debug_bundle_<cid>.md in the cwd."),
    ] = None,
) -> None:
    """Build a paste-ready debug bundle for CORRELATION_ID and write it to disk."""
    settings = get_settings()
    output_path = out or Path(f"debug_bundle_{correlation_id}.md")

    async def _run() -> None:
        builder = TrailBuilder(settings)
        bundle = await builder.build(correlation_id)
        output_path.write_text(bundle.to_markdown(), encoding="utf-8")

    asyncio.run(_run())
    typer.echo(f"wrote {output_path}")


def _collect_files(path: Path, *, recursive: bool) -> list[Path]:
    if path.is_file():
        return [path]
    pattern = "**/*" if recursive else "*"
    return sorted(p for p in path.glob(pattern) if p.is_file())


async def _ingest_paths(container: Container, files: list[Path], *, wait: bool) -> None:
    settings = container.settings
    doc_ids: list[str] = []
    for file_path in files:
        raw = file_path.read_bytes()
        mime = magic.from_buffer(raw, mime=True)
        if mime not in settings.limits.allowed_upload_mimetypes:
            typer.echo(f"skipping {file_path} (unsupported content type: {mime})")
            continue
        doc_id = document_id(raw)
        sha256 = document_sha256(raw)
        uri = persist_upload(raw, doc_id)
        is_new = await container.ledger.register(doc_id, uri, sha256, mime)
        if is_new:
            cid = new_correlation_id()
            await container.job_queue.enqueue(
                "ingest_document",
                JobEnvelope(
                    correlation_id=cid,
                    otel={},
                    enqueued_at=datetime.now(UTC),
                    payload=IngestDocumentPayload(
                        doc_id=doc_id, uri=uri, sha256=sha256, mime_type=mime
                    ),
                ),
                job_id=doc_id,
            )
            typer.echo(f"enqueued {file_path} -> doc_id={doc_id} cid={cid}")
        else:
            typer.echo(f"{file_path} already ingested -> doc_id={doc_id}")
        doc_ids.append(doc_id)

    if wait:
        await _wait_for_indexed(container, doc_ids)


async def _wait_for_indexed(container: Container, doc_ids: list[str]) -> None:
    """Poll the ledger until every doc_id reaches INDEXED or FAILED.

    Note: until BO-07 (extraction/resolution) lands, no worker function consumes the
    `extract_entities` jobs `IngestionService` enqueues, so a document's status will stall at
    EXTRACTING rather than ever reaching INDEXED. `--wait` still polls faithfully per BLUEPRINT
    §7.3 — Ctrl+C to stop.
    """
    pending = set(doc_ids)
    while pending:
        await asyncio.sleep(1)
        for doc_id in list(pending):
            record = await container.ledger.get(doc_id)
            if record is None:
                continue
            if record.status in (DocumentStatus.INDEXED, DocumentStatus.FAILED):
                typer.echo(f"{doc_id}: {record.status.value}")
                pending.discard(doc_id)


@app.command()
def ingest(
    path: Annotated[Path, typer.Argument(help="File or directory to ingest.")],
    recursive: Annotated[
        bool, typer.Option("--recursive", help="Recurse into subdirectories.")
    ] = False,
    wait: Annotated[
        bool,
        typer.Option("--wait", help="Block until each document reaches INDEXED (or FAILED)."),
    ] = False,
) -> None:
    """Ingest a file or directory through the same pipeline as `POST /v1/documents`."""
    settings = get_settings()
    files = _collect_files(path, recursive=recursive)

    async def _run() -> None:
        container = await Container.create(settings)
        try:
            await _ingest_paths(container, files, wait=wait)
        finally:
            await container.aclose()

    asyncio.run(_run())


@app.command()
def seed() -> None:
    """Ingest `corpus/` (MANUAL M-2's seed corpus)."""
    settings = get_settings()
    files = _collect_files(Path("corpus"), recursive=True)

    async def _run() -> None:
        container = await Container.create(settings)
        try:
            await _ingest_paths(container, files, wait=False)
        finally:
            await container.aclose()

    asyncio.run(_run())


@app.command()
def query(
    text: Annotated[str, typer.Argument(help="Query text.")],
    show_chunk_ids: Annotated[
        bool,
        typer.Option("--show-chunk-ids", help="Print each retrieved chunk's UUID beside its text."),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print raw JSON instead of a plain list.")
    ] = False,
    strategy: Annotated[
        str,
        typer.Option(
            "--strategy",
            help="Retrieval strategy. Only 'vector' runs until BO-09/10 land graph "
            "retrieval, fusion, and orchestration.",
        ),
    ] = "vector",
    top_k: Annotated[
        int | None, typer.Option("--top-k", help="Override retrieval.vector.top_k.")
    ] = None,
) -> None:
    """Direct hybrid-search debug query: embeds TEXT and runs one Qdrant hybrid_search call.

    NOT the full self-correcting orchestration pipeline — see this module's docstring.
    """
    if strategy != "vector":
        typer.echo(
            f"strategy={strategy!r} is not implemented until BO-09/10; using vector", err=True
        )

    settings = get_settings()

    async def _run() -> None:
        container = await Container.create(settings)
        try:
            embedder = container.embedder
            vector_store = container.vector_store
            assert embedder is not None
            assert vector_store is not None
            [dense] = await embedder.embed_dense([text], is_query=True)
            [sparse] = await embedder.embed_sparse([text])
            k = top_k or settings.retrieval.vector.top_k
            results = await vector_store.hybrid_search(
                dense=dense,
                sparse=sparse,
                top_k=k,
                prefetch_limit=settings.retrieval.vector.prefetch_limit,
                rrf_k=settings.retrieval.fusion.rrf_k,
                weights=None,
            )
            if as_json:
                payload = [
                    {
                        "chunk_id": str(r.chunk.chunk_id),
                        "score": r.score,
                        "text": r.chunk.text,
                    }
                    for r in results
                ]
                typer.echo(json.dumps(payload, indent=2))
            else:
                for r in results:
                    prefix = f"[{r.chunk.chunk_id}] " if show_chunk_ids else ""
                    snippet = r.chunk.text[:200].replace("\n", " ")
                    typer.echo(f"{prefix}({r.score:.4f}) {snippet}")
        finally:
            await container.aclose()

    asyncio.run(_run())


@app.command()
def reindex(
    force: Annotated[
        bool, typer.Option("--force", help="Accepted for contract completeness; see notes.")
    ] = False,
    corpus_dir: Annotated[
        Path,
        typer.Option("--corpus-dir", help="Where to re-derive the chunk_id set from."),
    ] = Path("corpus"),
) -> None:
    """Re-project Qdrant payloads from Postgres — the reconciliation entrypoint.

    `core.ports.SourceRegistry`/`VectorStore` expose no "list every known chunk_id" method (a
    BO-05 spec gap, reported alongside the rest of this build), so there is no way to ask either
    store for the full set to reconcile. This re-derives it the same deterministic way ingestion
    produced it in the first place: re-parsing and re-chunking CORPUS_DIR. Content addressing
    guarantees the recomputed chunk_ids match whatever was actually ingested from those files —
    `ProjectionService.project()` is convergent regardless of whether anything was actually
    corrupted, so `--force` has no separate code path; it's accepted so the flag from
    BLUEPRINT §7.3's command table means something if a future BO adds a cheaper skip-if-clean
    check.
    """
    del force  # see docstring — no separate code path yet
    settings = get_settings()
    parser = DocumentParser()
    files = _collect_files(corpus_dir, recursive=True)

    async def _run() -> None:
        container = await Container.create(settings)
        try:
            all_ids = set()
            for file_path in files:
                raw = file_path.read_bytes()
                mime = magic.from_buffer(raw, mime=True)
                if mime not in settings.limits.allowed_upload_mimetypes:
                    continue
                parsed = parser.parse(raw, mime)
                specs = chunk_document(
                    parsed,
                    chunk_size=settings.ingestion.chunk_size,
                    chunk_overlap=settings.ingestion.chunk_overlap,
                    min_chunk_chars=settings.ingestion.min_chunk_chars,
                )
                all_ids.update(chunk_id(spec.text) for spec in specs)

            assert container.vector_store is not None
            projection = ProjectionService(
                source_registry=container.sources, vector_store=container.vector_store
            )
            await projection.project(list(all_ids))
            typer.echo(f"re-projected {len(all_ids)} chunk(s) from {len(files)} file(s)")
        finally:
            await container.aclose()

    asyncio.run(_run())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
