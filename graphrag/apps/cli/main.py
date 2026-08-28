"""Typer CLI. See BLUEPRINT §7.3.

Only the `trail` command exists in this build order. `ingest`, `query`, `eval`, `reindex`,
`seed`, and `config-hash` land in later build orders (BO-05, BO-11) — adding stubs for them now
would be building ahead of the current BO.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from graphrag.adapters.telemetry.trail import TrailBuilder
from graphrag.config.settings import get_settings

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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
