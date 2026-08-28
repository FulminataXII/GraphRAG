"""Fixtures for tests marked `integration` — real docker-compose backends. Requires `make up`
(and, for Postgres-backed tests, `make migrate`) to have already run; see BLUEPRINT §9 /
MANUAL.md.

`config/local.yaml` already points `stores.qdrant`/`stores.neo4j`/`stores.redis` at localhost
for exactly this reason (BO-03's addition — see that file's comment), so a plain `Settings()`
constructed here (no APP_ENV override; it defaults to "local") resolves those three correctly.
Only the Postgres DSN needs a host override: it lives in `secrets`, which is populated ONLY from
env/.env and has no YAML leaf to override.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from graphrag.adapters.postgres.tables import SCHEMA

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def _read_dotenv(key: str, default: str) -> str:
    if not _ENV_PATH.is_file():
        return default
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return default


POSTGRES_DSN_LOCAL = (
    f"postgresql+asyncpg://{_read_dotenv('POSTGRES_USER', 'graphrag')}:"
    f"{_read_dotenv('POSTGRES_PASSWORD', 'changeme')}@localhost:5432/"
    f"{_read_dotenv('POSTGRES_DB', 'graphrag')}"
)
REDIS_URL_LOCAL = "redis://localhost:6379/0"


@pytest.fixture
async def pg_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(POSTGRES_DSN_LOCAL)
    yield engine
    await engine.dispose()


@pytest.fixture
async def clean_pg(pg_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """Truncates BO-03's tables before AND after each test, so tests don't leak state into
    each other regardless of run order."""

    async def _truncate() -> None:
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(f"TRUNCATE TABLE {SCHEMA}.documents, {SCHEMA}.chunk_sources RESTART IDENTITY")
            )
            await conn.execute(
                text(f"UPDATE {SCHEMA}.corpus_version_counter SET value = 0 WHERE id = 1")
            )

    await _truncate()
    yield pg_engine
    await _truncate()


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL_LOCAL)
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def stack() -> None:
    """Marker fixture: depending on it documents "this test needs `make up`" without pulling in
    heavier per-backend fixtures a given test doesn't need. See MANUAL.md / BUILD_ORDER.md."""
    return None
