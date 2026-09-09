"""PostgresEvalStore — reader/writer for eval_runs and eval_results. See BLUEPRINT §8.

Uses the table stubs defined in ``adapters.postgres.tables`` (BO-03 schema). This is the
first module to actually read/write those tables.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import insert, select

from graphrag.adapters.postgres.tables import eval_results, eval_runs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

_log = logging.getLogger(__name__)


class PostgresEvalStore:
    """Persist eval runs and per-item results to Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def save_run(
        self,
        *,
        run_id: str,
        git_sha: str,
        config_hash: str,
        started_at: Any,
        finished_at: Any,
        metrics: dict[str, Any],
    ) -> None:
        """Insert a single eval_runs row."""
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(eval_runs).values(
                    run_id=run_id,
                    git_sha=git_sha,
                    config_hash=config_hash,
                    started_at=started_at,
                    finished_at=finished_at,
                    metrics=json.loads(json.dumps(metrics, default=str)),
                )
            )

    async def save_results(self, *, run_id: str, results: list[dict[str, Any]]) -> None:
        """Bulk insert eval_results rows for one run."""
        if not results:
            return
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(eval_results),
                [
                    {
                        "run_id": run_id,
                        "item_id": r.get("item_id", ""),
                        "result": json.loads(json.dumps(r, default=str)),
                    }
                    for r in results
                ],
            )

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Fetch a single eval_runs row by run_id."""
        async with self._engine.connect() as conn:
            row = (
                (await conn.execute(select(eval_runs).where(eval_runs.c.run_id == run_id)))
                .mappings()
                .first()
            )
            if row is None:
                return None
            return dict(row)
