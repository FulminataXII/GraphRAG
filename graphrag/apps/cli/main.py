"""Typer CLI. See BLUEPRINT §7.3.

BO-02 built `trail`. This BO adds `ingest`, `seed`, `query --show-chunk-ids`, and `reindex`
(exactly BUILD_ORDER.md's BO-05 list). `eval` and `config-hash` land in later build orders —
adding stubs for them now would be building ahead of the current BO.

`query` is a direct hybrid-search debug tool, not the full self-correcting orchestration
pipeline (`POST /v1/query`) — that depends on `services/retrieval` and `services/orchestration`,
which don't exist until BO-09/10. It exists now because MANUAL M-5 needs it before BO-11:
`--show-chunk-ids` is how `gold_chunk_ids` get collected without anyone transcribing a UUID off
a screen.

`node` (added for the same reason, one BO later) runs ONE orchestration node against real
`NodeDeps` and prints what it returned plus the LLM calls it made. BLUEPRINT §6.6 calls the
nodes independently testable — they are plain `async def node(state, deps) -> dict` — but
nothing exposed that to a person, so tuning a router or grader prompt meant a full end-to-end
query: four LLM calls and ~25s per iteration. See `node`'s own docstring for the prefill rules.
"""

from __future__ import annotations

import asyncio
import json
import socket
import textwrap
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Protocol, runtime_checkable
from urllib.parse import urlparse
from uuid import UUID

import magic
import typer
from pydantic import BaseModel, SecretStr
from pydantic_core import to_jsonable_python
from sqlalchemy.exc import DBAPIError

from graphrag.adapters.telemetry.trail import TrailBuilder
from graphrag.apps._upload_storage import persist_upload
from graphrag.apps.api.main import Container
from graphrag.config.settings import Settings, get_settings
from graphrag.core.errors import ConflictError
from graphrag.core.events import INGEST_DOCUMENT, IngestDocumentPayload, JobEnvelope
from graphrag.core.ids import chunk_id, new_correlation_id
from graphrag.core.models import DocumentStatus, GraphPath, ScoredChunk, Spend
from graphrag.core.ports import LLMClient
from graphrag.services.ingestion.chunker import chunk_document
from graphrag.services.ingestion.parser import DocumentParser
from graphrag.services.ingestion.service import ProjectionService, document_id, document_sha256
from graphrag.services.orchestration.nodes import (
    finalize,
    fuse,
    generate,
    grade_context,
    guard,
    insufficient,
    plan_route,
    repair,
    retrieve_graph,
    retrieve_vector,
    rewrite_query,
    verify_citations,
    verify_grounded,
)
from graphrag.services.orchestration.state import merge_counters

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


# ---------------------------------------------------------------------------
# host-side settings
# ---------------------------------------------------------------------------
#: Compose service names that only resolve from INSIDE the compose network. `config/local.yaml`
#: already rewrites `stores.qdrant`/`stores.neo4j`/`stores.redis` and `llm.gateway_base_url` to
#: the published localhost ports for exactly this reason. Postgres cannot be rewritten there:
#: its DSN lives in `secrets`, which is populated ONLY from env/.env and has no YAML leaf — so
#: the same rewrite has to happen here, in the one process that is always host-side.
_COMPOSE_ONLY_HOSTS: Final[frozenset[str]] = frozenset({"postgres", "qdrant", "neo4j", "redis"})


def _resolves(hostname: str) -> bool:
    try:
        socket.getaddrinfo(hostname, None)
    except OSError:
        return False
    return True


def cli_settings() -> Settings:
    """`get_settings()`, with the Postgres DSN pointed somewhere this process can actually reach.

    Every CLI command runs on the HOST, outside the compose network, where `.env`'s
    `postgresql://...@postgres:5432/graphrag` cannot resolve — and the failure was a raw
    `gaierror` from deep inside asyncpg, which says nothing about what to do. `make migrate`
    works around it by exporting a localhost DSN inline; requiring that of every `graphrag node`
    invocation is ceremony M-5 would pay fifty times over.

    The rewrite is conditional on the name not resolving, so it cannot hijack a DSN that points
    at a real host, and it announces itself on stderr rather than silently changing where a
    command writes.
    """
    settings = get_settings()
    dsn = settings.secrets.postgres_dsn.get_secret_value()
    hostname = urlparse(dsn).hostname
    if hostname is None or hostname not in _COMPOSE_ONLY_HOSTS or _resolves(hostname):
        return settings

    rewritten = dsn.replace(f"@{hostname}:", "@localhost:", 1)
    typer.echo(
        f"note: postgres host {hostname!r} is a compose service name and does not resolve on "
        "this machine; using localhost instead",
        err=True,
    )
    return settings.model_copy(
        update={
            "secrets": settings.secrets.model_copy(update={"postgres_dsn": SecretStr(rewritten)})
        }
    )


async def open_container(settings: Settings) -> Container:
    """`Container.create`, with a connection failure translated into an actionable message.

    `Container.create` opens Postgres, Redis, Qdrant and Neo4j clients; when the stack is down
    the first one to fail surfaces as a bare `OSError`/`DBAPIError` under a Typer traceback,
    which reads as a bug in the CLI rather than "the containers aren't running".
    """
    try:
        return await Container.create(settings)
    except (OSError, DBAPIError) as exc:
        typer.echo(
            f"cannot reach a backing store: {type(exc).__name__}: {str(exc).splitlines()[0]}\n"
            "The CLI talks to the docker-compose stack over the published localhost ports. "
            "Start it with `make up` (add `obs=1` for the observability plane) and, if this is "
            "a fresh database, `make migrate`.",
            err=True,
        )
        raise typer.Exit(code=1) from None


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
    settings = cli_settings()
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


@runtime_checkable
class _PurgeableLedger(Protocol):
    """A ledger that can drop a row outright. Structural, not the concrete adapter class:
    `purge` is deliberately absent from `core.ports.DocumentLedger` (BLUEPRINT §3.5), but
    `--force` still has to be testable against a fake rather than only against real Postgres."""

    async def purge(self, doc_id: str) -> bool: ...


@runtime_checkable
class _DroppableQueue(Protocol):
    """A queue that can clear arq's retained per-job keys. See `_PurgeableLedger`."""

    async def drop_job(self, job_id: str) -> None: ...


async def _reset_for_force(
    container: Container, doc_id: str, file_path: Path, *, even_if_indexed: bool
) -> bool:
    """Clear everything that would make a re-ingest of `doc_id` a no-op. True if it may proceed.

    Two independent things suppress a re-ingest, and repairing a partial ingest needs both gone:
    the ledger row (`register()` dedups on sha256 regardless of status, so a FAILED document is
    skipped exactly like a healthy one) and arq's retained per-job keys (a finished job's result
    is kept for `keep_result`, during which re-enqueueing that job_id is silently dropped).

    Refuses an INDEXED document unless `even_if_indexed`. That guard is the point of the
    command: `--force` re-pays full extraction quota for every document it touches, so pointing
    it at a healthy corpus by accident must not be one flag away.
    """
    ledger = container.ledger
    job_queue = container.job_queue
    if not isinstance(ledger, _PurgeableLedger) or not isinstance(job_queue, _DroppableQueue):
        raise typer.BadParameter(
            "--force needs a ledger with purge() and a queue with drop_job(); this container "
            f"has {type(ledger).__name__}/{type(job_queue).__name__}"
        )

    record = await container.ledger.get(doc_id)
    if record is not None and record.status is DocumentStatus.INDEXED and not even_if_indexed:
        typer.echo(
            f"refusing {file_path} -> doc_id={doc_id} is INDEXED "
            f"(pass --even-if-indexed to re-ingest it anyway; this re-pays extraction quota)"
        )
        return False

    purged = await ledger.purge(doc_id)
    await job_queue.drop_job(doc_id)
    was = record.status.value if record is not None else "no ledger row"
    typer.echo(f"forcing {file_path} -> doc_id={doc_id} (was {was}, row purged={purged})")
    return True


async def _ingest_paths(
    container: Container,
    files: list[Path],
    *,
    wait: bool,
    force: bool = False,
    even_if_indexed: bool = False,
) -> None:
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
        if force and not await _reset_for_force(
            container, doc_id, file_path, even_if_indexed=even_if_indexed
        ):
            continue
        is_new = await container.ledger.register(doc_id, uri, sha256, mime)
        if is_new:
            cid = new_correlation_id()
            try:
                await container.job_queue.enqueue(
                    INGEST_DOCUMENT,
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
            except ConflictError as exc:
                # The ledger row is new but arq still holds keys for this doc_id. Report the
                # remedy rather than a traceback -- an unreported drop here is exactly the
                # failure this exception was added to make visible.
                typer.echo(f"NOT enqueued {file_path} -> doc_id={doc_id}: {exc.message}")
                continue
            typer.echo(f"enqueued {file_path} -> doc_id={doc_id} cid={cid}")
        else:
            record = await container.ledger.get(doc_id)
            status = record.status.value if record is not None else "unknown"
            # Spell the status out. "already ingested" was printed for FAILED and stuck
            # in-progress documents too, so a broken corpus read as a clean one.
            typer.echo(f"skipped {file_path} -> doc_id={doc_id} already registered ({status})")
        doc_ids.append(doc_id)

    if wait:
        await _wait_for_indexed(container, doc_ids)


async def _wait_for_indexed(container: Container, doc_ids: list[str]) -> None:
    """Poll the ledger until every doc_id reaches INDEXED or FAILED.

    Both are now genuinely reachable for every terminal outcome, which they were not before: a
    job cancelled by arq's `job_timeout` used to leave its row at EXTRACTING/RESOLVING forever
    and this loop would never return. `tasks/_common.py` records FAILED on cancellation, so the
    two states polled for here really do cover the terminal set. Ctrl+C to stop.
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
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Repair a partial ingest: purge the ledger row and arq's retained job keys for "
                "each document that is not INDEXED, then re-enqueue it. Re-pays extraction "
                "quota for every document it touches."
            ),
        ),
    ] = False,
    even_if_indexed: Annotated[
        bool,
        typer.Option(
            "--even-if-indexed",
            help="With --force, also re-ingest documents already at INDEXED. Rarely correct.",
        ),
    ] = False,
) -> None:
    """Ingest a file or directory through the same pipeline as `POST /v1/documents`.

    Without `--force` this is idempotent and cheap: a document whose content is already
    registered is skipped, whatever state it is in. That is also its limitation — a document
    that FAILED, or that was cancelled mid-extraction, is skipped identically to a healthy one,
    so re-running a folder never repairs a partial ingest. `--force` is how you repair one.
    """
    settings = cli_settings()
    files = _collect_files(path, recursive=recursive)
    if even_if_indexed and not force:
        raise typer.BadParameter("--even-if-indexed only means anything together with --force")

    async def _run() -> None:
        container = await open_container(settings)
        try:
            await _ingest_paths(
                container, files, wait=wait, force=force, even_if_indexed=even_if_indexed
            )
        finally:
            await container.aclose()

    asyncio.run(_run())


@app.command()
def seed() -> None:
    """Ingest `corpus/` (MANUAL M-2's seed corpus)."""
    settings = cli_settings()
    files = _collect_files(Path("corpus"), recursive=True)

    async def _run() -> None:
        container = await open_container(settings)
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

    settings = cli_settings()

    async def _run() -> None:
        container = await open_container(settings)
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
    settings = cli_settings()
    parser = DocumentParser()
    files = _collect_files(corpus_dir, recursive=True)

    async def _run() -> None:
        container = await open_container(settings)
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


# ---------------------------------------------------------------------------
# `node` — run ONE orchestration node by hand. See this module's docstring.
# ---------------------------------------------------------------------------

#: Every node in BLUEPRINT §6.4's graph, by the name `build_query_graph` registers it under.
#: Imported explicitly rather than walked: `nodes/` is a namespace package, so an attribute
#: lookup would only find the submodules some other import happened to have loaded already.
_NODES: Final[dict[str, Any]] = {
    "guard": guard,
    "plan_route": plan_route,
    "retrieve_vector": retrieve_vector,
    "retrieve_graph": retrieve_graph,
    "fuse": fuse,
    "grade_context": grade_context,
    "rewrite_query": rewrite_query,
    "generate": generate,
    "verify_citations": verify_citations,
    "verify_grounded": verify_grounded,
    "repair": repair,
    "finalize": finalize,
    "insufficient": insufficient,
}

#: Which upstream nodes have to run before the target node's inputs are valid. Derived from
#: BLUEPRINT §6.4's edge list, not invented: it is the path from START to the target, minus the
#: branches a straight-line hand run never takes (`rewrite_query`, `repair`).
#:
#: `grade_context` is deliberately NOT on any path. Grading is itself a graded LLM call, and a
#: node being iterated against a hand-picked context wants that context ungraded — so after
#: `fuse`, `graded` is seeded from `fused` and the substitution is printed.
_UPSTREAM: Final[dict[str, tuple[str, ...]]] = {
    "guard": (),
    "plan_route": (),
    "rewrite_query": (),
    "finalize": (),
    "retrieve_vector": ("plan_route",),
    "retrieve_graph": ("plan_route",),
    "fuse": ("plan_route", "retrieve_vector", "retrieve_graph"),
    "grade_context": ("plan_route", "retrieve_vector", "retrieve_graph", "fuse"),
    "generate": ("plan_route", "retrieve_vector", "retrieve_graph", "fuse"),
    "insufficient": ("plan_route", "retrieve_vector", "retrieve_graph", "fuse"),
    "verify_citations": ("plan_route", "retrieve_vector", "retrieve_graph", "fuse", "generate"),
    "verify_grounded": ("plan_route", "retrieve_vector", "retrieve_graph", "fuse", "generate"),
    "repair": ("plan_route", "retrieve_vector", "retrieve_graph", "fuse", "generate"),
}

#: Upstream nodes that only exist to produce retrieved context. `--chunk-ids` supplies that
#: context directly, so these are skipped when it is given.
_RETRIEVAL_NODES: Final[frozenset[str]] = frozenset(
    {"plan_route", "retrieve_vector", "retrieve_graph", "fuse"}
)


class _RecordingLLM:
    """Delegates to the real client and remembers what each call cost.

    `StructuredResult` already carries `model_served`/tokens/latency — `model_served` is how a
    provider fallback becomes visible, since it is the model the gateway ACTUALLY served, not
    the alias asked for. What it does not carry is the role or the alias, which is why this
    wrapper (which knows both) exists rather than the command reading the result alone.
    """

    def __init__(self, inner: LLMClient, llm_settings: Any) -> None:
        self._inner = inner
        self._llm = llm_settings
        self.calls: list[dict[str, Any]] = []

    async def structured(
        self, *, role: str, messages: list[dict[str, str]], schema: type, max_repairs: int
    ) -> Any:
        alias = self._llm.roles[role].model
        started = time.perf_counter()
        try:
            result = await self._inner.structured(
                role=role, messages=messages, schema=schema, max_repairs=max_repairs
            )
        except Exception as exc:
            self.calls.append(
                {
                    "role": role,
                    "alias": alias,
                    "model_served": None,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "repair_attempts": None,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            raise
        self.calls.append(
            {
                "role": role,
                "alias": alias,
                "model_served": result.model_served,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "repair_attempts": result.repair_attempts,
                "latency_ms": result.latency_ms,
                "error": None,
            }
        )
        return result

    def stream_text(self, *, role: str, messages: list[dict[str, str]]) -> Any:
        return self._inner.stream_text(role=role, messages=messages)

    async def health(self) -> bool:
        return await self._inner.health()


def _seed_state(question: str, top_k: int | None, strategy: str | None) -> dict[str, Any]:
    """The same channel seed `OrchestrationService.run` builds. Kept identical on purpose: a
    node inspected here must see the state shape it sees in production."""
    return {
        "correlation_id": new_correlation_id(),
        "question": question,
        "active_query": question,
        "plan": None,
        "top_k": top_k,
        "strategy_override": strategy,
        "vector_hits": [],
        "graph_hits": [],
        "fused": [],
        "graded": [],
        "answer": None,
        "degraded": [],
        "failures": [],
        "attempts": {},
        "spent": Spend(),
    }


def _apply(state: dict[str, Any], update: dict[str, Any]) -> None:
    """Merge a node's partial update into the state using QueryState's own reducers.

    LangGraph applies these; running a node outside the graph means applying them here. Getting
    them wrong would make a prefilled state diverge from the one production builds — `attempts`
    especially, which several nodes read to number their own failures.
    """
    for key, value in update.items():
        if key == "spent":
            state["spent"] = Spend.merge(state.get("spent", Spend()), value)
        elif key == "attempts":
            state["attempts"] = merge_counters(state.get("attempts", {}), value)
        elif key in ("degraded", "failures"):
            state[key] = state.get(key, []) + value
        else:
            state[key] = value


def _summarize(update: dict[str, Any]) -> str:
    """Human-readable rendering of a partial state update.

    Chunk lists are printed as one line each rather than dumped whole: `fused` alone is ten
    chunks of up to 900 characters, which buries the field a person is actually reading. `--json`
    prints everything.
    """
    lines: list[str] = []
    for key, value in update.items():
        if isinstance(value, list) and value and isinstance(value[0], ScoredChunk):
            lines.append(f"{key}: {len(value)} chunk(s)")
            for scored in value:
                text = scored.chunk.text[:110].replace("\n", " ")
                lines.append(
                    f"  [{scored.chunk.chunk_id}] rank={scored.rank} "
                    f"score={scored.score:.4f} origin={scored.origin} {text}"
                )
        elif isinstance(value, list) and value and isinstance(value[0], GraphPath):
            lines.append(f"{key}: {len(value)} path(s)")
            for path in value:
                lines.append(f"  score={path.score:.4f} chunks={len(path.chunks)}")
        elif isinstance(value, BaseModel):
            lines.append(f"{key}:")
            lines.append(textwrap.indent(json.dumps(value.model_dump(mode="json"), indent=2), "  "))
        else:
            lines.append(f"{key}: {json.dumps(to_jsonable_python(value))}")
    return "\n".join(lines)


#: A LiteLLM gateway error carries the whole fallback chain's nested exception text — several
#: thousand characters that push the rest of the run off the screen. `--json` keeps it whole.
_ERROR_PREVIEW_CHARS: Final[int] = 400


def _print_calls(calls: list[dict[str, Any]]) -> None:
    if not calls:
        typer.echo("llm calls: none")
        return
    typer.echo(f"llm calls: {len(calls)}")
    for call in calls:
        if call["error"] is not None:
            message = " ".join(call["error"].split())
            if len(message) > _ERROR_PREVIEW_CHARS:
                message = f"{message[:_ERROR_PREVIEW_CHARS]}... (--json for the full error)"
            typer.echo(
                f"  role={call['role']} alias={call['alias']} "
                f"latency={call['latency_ms']}ms FAILED {message}"
            )
            continue
        typer.echo(
            f"  role={call['role']} alias={call['alias']} "
            f"model_served={call['model_served']} "
            f"tokens={call['prompt_tokens']}p/{call['completion_tokens']}c "
            f"repairs={call['repair_attempts']} latency={call['latency_ms']}ms"
        )


@app.command()
def node(
    name: Annotated[str, typer.Argument(help="Node to run, e.g. plan_route / grade_context.")],
    question: Annotated[str, typer.Option("--question", help="The user's question.")],
    chunk_ids: Annotated[
        str | None,
        typer.Option(
            "--chunk-ids",
            help="Comma-separated chunk UUIDs to use as the context, instead of retrieving.",
        ),
    ] = None,
    top_k: Annotated[
        int | None, typer.Option("--top-k", help="Override retrieval.vector.top_k.")
    ] = None,
    strategy: Annotated[
        str | None,
        typer.Option("--strategy", help="strategy_override: vector | graph | hybrid."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the full update and call log as JSON.")
    ] = False,
) -> None:
    """Run ONE orchestration node against real dependencies and print what it returned.

    Nodes are `async def node(state, deps) -> dict` (BLUEPRINT §6.6); this constructs a valid
    `QueryState`, calls one, and prints the partial update plus every LLM call made — role,
    alias, model ACTUALLY served (which is how a provider fallback becomes visible), token
    counts and latency.

    A node downstream of retrieval needs retrieved context to be worth running, so the upstream
    nodes that produce it are run first and named in the output; `--chunk-ids` supplies that
    context directly instead. Retrieval spends no LLM quota here (fastembed and both stores are
    local), so the whole prefill costs at most the one `plan_route` call at `fast-low-latency`.

    There is no `--chunks-from <correlation_id>`, though iterating on a past query is the
    obvious thing to want: NOTHING in this system is keyed by correlation_id — no Postgres row,
    no Redis key, no span attribute carrying the question or the chunk ids. Reported as a spec
    gap rather than worked around here.
    """
    if name not in _NODES:
        raise typer.BadParameter(f"unknown node {name!r}; one of: {', '.join(sorted(_NODES))}")
    if strategy is not None and strategy not in ("vector", "graph", "hybrid"):
        raise typer.BadParameter(f"strategy must be vector|graph|hybrid, got {strategy!r}")

    pinned = [UUID(value.strip()) for value in chunk_ids.split(",")] if chunk_ids else []
    settings = cli_settings()

    async def _run() -> None:
        container = await open_container(settings)
        try:
            assert container.orchestrator is not None
            recorder = _RecordingLLM(container.orchestrator.deps.llm, settings.llm)
            deps = container.orchestrator.deps.model_copy(update={"llm": recorder})

            state = _seed_state(question, top_k, strategy)
            upstream = _UPSTREAM[name]
            if pinned:
                assert container.vector_store is not None
                chunks = await container.vector_store.get_chunks(pinned)
                missing = set(pinned) - {c.chunk_id for c in chunks}
                if missing:
                    typer.echo(
                        f"warning: {len(missing)} chunk id(s) not in "
                        f"{settings.retrieval.vector.collection}: "
                        f"{', '.join(str(m) for m in sorted(missing, key=str))}",
                        err=True,
                    )
                scored = [
                    ScoredChunk(chunk=chunk, score=1.0, rank=i, origin="vector")
                    for i, chunk in enumerate(chunks, start=1)
                ]
                state["vector_hits"] = scored
                state["fused"] = scored
                state["graded"] = scored
                upstream = tuple(n for n in upstream if n not in _RETRIEVAL_NODES)
                typer.echo(f"context: {len(scored)} pinned chunk(s)")

            if upstream:
                typer.echo(f"prefill: {' -> '.join(upstream)}")
            for upstream_name in upstream:
                _apply(state, await _NODES[upstream_name].node(state, deps))
                if upstream_name == "fuse":
                    # `generate`/`verify_*` read `graded`, and grading is an LLM call whose
                    # result would be this run's input rather than its subject. Substitute and
                    # say so, rather than handing the node an empty context.
                    state["graded"] = list(state["fused"])
                    typer.echo(f"prefill: graded := fused ({len(state['graded'])} chunk(s))")

            prefill_calls = list(recorder.calls)
            recorder.calls.clear()

            typer.echo(f"\n=== {name} ===")
            # A node that raises is a RESULT here, not a crash: `generate` letting an
            # LLMSchemaViolation out is exactly the kind of thing this command exists to show,
            # and Typer's traceback would bury the call log that says which role and which model
            # produced it. Print the log, then exit non-zero.
            try:
                update = await _NODES[name].node(state, deps)
            except Exception as exc:
                typer.echo(f"raised {type(exc).__name__}: {exc}", err=True)
                typer.echo("")
                _print_calls(recorder.calls)
                raise typer.Exit(code=1) from None

            if as_json:
                typer.echo(
                    json.dumps(
                        {
                            "node": name,
                            "correlation_id": state["correlation_id"],
                            "prefill": list(upstream),
                            "prefill_llm_calls": prefill_calls,
                            "update": to_jsonable_python(update),
                            "llm_calls": recorder.calls,
                        },
                        indent=2,
                    )
                )
                return

            typer.echo(_summarize(update) or "(empty update)")
            typer.echo("")
            _print_calls(recorder.calls)
            if prefill_calls:
                typer.echo(f"(prefill additionally made {len(prefill_calls)} llm call(s))")
        finally:
            await container.aclose()

    asyncio.run(_run())


@app.command()
def eval(
    subset: Annotated[
        bool,
        typer.Option("--subset", help="Run evaluation.smoke_subset_size items for CI."),
    ] = False,
    report: Annotated[
        Path | None,
        typer.Option("--report", help="Path to write the markdown report."),
    ] = None,
) -> None:
    """Run the evaluation pipeline against the golden set.

    Loads the golden set, runs each item through orchestration, computes metrics, and
    checks thresholds. Exits 1 if any metric is below evaluation.thresholds.
    """
    settings = cli_settings()

    async def _run() -> None:
        container = await open_container(settings)
        try:
            assert container.orchestrator is not None

            # Import here to avoid circular imports and unnecessary dependency loading
            from graphrag.evaluation.metrics.generation import GenerationJudge
            from graphrag.evaluation.report import render_markdown
            from graphrag.evaluation.runner import EvalRunner

            assert container.embedder is not None
            judge = GenerationJudge(llm_client=container.llm_client, embedder=container.embedder)

            subset_size = settings.evaluation.smoke_subset_size if subset else None

            runner = EvalRunner(
                settings=settings,
                orchestrator=container.orchestrator,
                judge=judge,
            )
            eval_report = await runner.run(
                subset=subset_size,
                include_generation_metrics=True,
            )

            # Print summary
            md = render_markdown(eval_report)
            typer.echo(md)

            # Write report file
            report_path = report
            if report_path is None:
                report_dir = Path(settings.evaluation.report_path)
                report_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
                report_path = report_dir / f"eval_{eval_report.run_id}.md"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(md, encoding="utf-8")
            typer.echo(f"\nreport written to {report_path}")

            if not eval_report.thresholds_passed:
                typer.echo("FAIL: one or more metrics below threshold", err=True)
                raise typer.Exit(code=1)

        finally:
            await container.aclose()

    asyncio.run(_run())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
