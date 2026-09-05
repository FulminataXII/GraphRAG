"""Fixtures for tests marked `integration` — real docker-compose backends. Requires `make up`;
`make test-int` additionally starts the isolated `neo4j-test` container these fixtures talk to.
See BLUEPRINT §9 / MANUAL.md.

**Every backend here is namespaced per run, not cleaned up after.** `tests/integration/
namespaces.py` owns the derivation and explains the incident that forced it; the short version
is that a fixture which truncates a shared store fails open on a crashed run and once destroyed
an ingested corpus. What changed:

    was                                       is
    ---                                       --
    TRUNCATE graphrag.documents in the         a `graphrag_test_<run>` database this session
    production database                        CREATEs and DROPs
    MATCH (n) DETACH DELETE n on the           a separate `neo4j-test` instance on port 7688
    production graph                           that holds nothing but this run's writes
    flushdb() on Redis db 0 (the arq queue     Redis db 1..15, and `flushdb` refuses db 0
    the real worker consumes)
    (nothing)                                  Qdrant collections prefixed `test_<run>_`

`config/local.yaml` already points `stores.qdrant`/`stores.neo4j`/`stores.redis` at localhost
(BO-03's addition — see that file's comment), so a plain `Settings()` here (no APP_ENV override;
it defaults to "local") resolves the HOSTS correctly. `namespaces.namespaced()` then swaps the
NAMES. Build test settings with it, never with a bare `Settings()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j.exceptions import ServiceUnavailable
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from graphrag.adapters.postgres.tables import SCHEMA
from graphrag.config.settings import Settings
from tests.integration import namespaces as ns
from tests.unit._settings_helpers import set_required_secrets

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Re-exported for tests that need the DSN without taking the fixture. Points at THIS RUN's
#: database, which does not exist until `_pg_database` has run.
POSTGRES_DSN_LOCAL = ns.POSTGRES_DSN


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
@pytest.fixture
def integration_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Real `Settings()`, namespaced to this run. The default way to get settings in an
    integration test — see this module's docstring for why a bare `Settings()` is not."""
    set_required_secrets(monkeypatch)
    return ns.namespaced(Settings())


# ---------------------------------------------------------------------------
# qdrant — collections this run created, and only those
# ---------------------------------------------------------------------------
async def drop_collections(client: AsyncQdrantClient, *names: str) -> None:
    """Teardown for collections a test created. The `owned_collection` assertion is the point:
    a fixture that somehow resolved to `chunks` deletes nothing and says so, instead of dropping
    the corpus. Qdrant is where the original incident left 129 orphaned points."""
    for name in names:
        assert ns.owned_collection(name), (
            f"refusing to delete {name!r}: not a collection this run created"
        )
        with contextlib.suppress(Exception):
            await client.delete_collection(name)


# ---------------------------------------------------------------------------
# postgres — a database this run creates and drops
# ---------------------------------------------------------------------------
async def _admin_execute(statement: str) -> None:
    """Run one statement on the maintenance database. AUTOCOMMIT because CREATE/DROP DATABASE
    cannot run inside a transaction block."""
    engine = create_async_engine(ns.POSTGRES_ADMIN_DSN, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(statement))
    finally:
        await engine.dispose()


def _migrate(dsn: str) -> None:
    """`alembic upgrade head` against `dsn`, in a subprocess.

    A subprocess, not `metadata.create_all()`: create_all reproduces only what `tables.py`
    expresses, so anything a migration does beyond that (0001 seeds the single
    `corpus_version_counter` row) would be silently missing, and the gap would widen with every
    future revision. This runs the same command `make migrate` does, so the tables under test
    are the tables production gets. Alembic's `env.py` resolves the DSN through
    `get_settings()`, hence the env override rather than a `-x` argument.
    """
    env = {**os.environ, "APP_ENV": "local", "GRAPHRAG_SECRETS__POSTGRES_DSN": dsn}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade head failed for {ns.POSTGRES_TEST_DB}:\n{result.stderr}"
        )


@pytest.fixture(scope="session")
def _pg_database() -> Iterator[str]:
    """CREATE the per-run database, migrate it, DROP it. Session-scoped and synchronous: the
    per-test fixtures below each open their own engine in their own event loop, so nothing
    created here is ever handed across loops."""
    assert ns.POSTGRES_TEST_DB != ns.PRODUCTION_POSTGRES_DB, (
        f"the per-run database resolved to the production one ({ns.POSTGRES_TEST_DB}); "
        "refusing to create or drop it"
    )
    asyncio.run(_admin_execute(f'DROP DATABASE IF EXISTS "{ns.POSTGRES_TEST_DB}" WITH (FORCE)'))
    asyncio.run(_admin_execute(f'CREATE DATABASE "{ns.POSTGRES_TEST_DB}"'))
    try:
        _migrate(ns.POSTGRES_DSN)
        yield ns.POSTGRES_DSN
    finally:
        asyncio.run(_admin_execute(f'DROP DATABASE IF EXISTS "{ns.POSTGRES_TEST_DB}" WITH (FORCE)'))


@pytest.fixture
async def pg_engine(_pg_database: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(_pg_database)
    yield engine
    await engine.dispose()


@pytest.fixture
async def clean_pg(pg_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """Truncates BO-03's tables before AND after each test, so tests don't leak state into each
    other regardless of run order.

    Truncating is safe here ONLY because `_pg_database` created the database this connects to
    minutes ago. The same statements against the shared production database are what emptied
    `graphrag.documents` — the isolation is the database name, not this cleanup."""

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


# ---------------------------------------------------------------------------
# redis — a logical db index, never 0
# ---------------------------------------------------------------------------
@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    client = Redis.from_url(ns.REDIS_URL)
    yield client
    # `flushdb` is why the index matters: on db 0 it wipes the arq queue the running worker is
    # consuming, plus every cached embedding. Assert rather than trust the URL.
    db_index = client.connection_pool.connection_kwargs.get("db", 0)
    assert db_index != ns.PRODUCTION_REDIS_DB_INDEX, (
        f"redis_client resolved to db {db_index}, which is production's; refusing to flush"
    )
    await client.flushdb()
    await client.aclose()


# ---------------------------------------------------------------------------
# neo4j — the separate `neo4j-test` instance on port 7688
# ---------------------------------------------------------------------------
@pytest.fixture
async def neo4j_driver() -> AsyncIterator[AsyncDriver]:
    driver = AsyncGraphDatabase.driver(ns.NEO4J_URI, auth=("neo4j", ns.NEO4J_PASSWORD))
    try:
        await driver.verify_connectivity()
    except ServiceUnavailable as exc:  # pragma: no cover — operator error, not a code path
        await driver.close()
        pytest.fail(
            f"no Neo4j at {ns.NEO4J_URI}. The integration suite deliberately does NOT use the "
            "production instance on 7687 — run `make neo4j-test` (or `make test-int`, which "
            f"does it for you) to start the isolated one. Original error: {exc}"
        )
    yield driver
    await driver.close()


@pytest.fixture
async def clean_neo4j(neo4j_driver: AsyncDriver) -> AsyncIterator[AsyncDriver]:
    """Deletes every node (and their relationships) before AND after each test, so tests don't
    leak graph state into each other regardless of run order — same convention as `clean_pg`.

    `MATCH (n) DETACH DELETE n` is unchanged from the version that destroyed a corpus; what
    changed is what it is pointed at. `neo4j_driver` addresses port 7688, the `neo4j-test`
    container, which nothing but this suite ever writes to."""

    async def _wipe() -> None:
        await neo4j_driver.execute_query("MATCH (n) DETACH DELETE n", database_="neo4j")

    await _wipe()
    yield neo4j_driver
    await _wipe()


# ---------------------------------------------------------------------------
# end-of-run sweep
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _drop_run_namespaces() -> Iterator[None]:
    """At session end, remove whatever still carries this run's namespace.

    Per-test fixtures drop what they explicitly created, but several create a chunks/entities
    PAIR via `ensure_collections()` and name only one of them on the way out, and a crashed test
    drops nothing at all. Both leave debris — the `test_ing_chunks_*` collections found orphaned
    after the incident were exactly this. A prefix sweep is safe precisely because the prefix is
    per-run: it cannot select anything production owns.
    """
    yield
    asyncio.run(_sweep())


async def _sweep() -> None:
    settings = ns.namespaced(Settings())
    client = AsyncQdrantClient(url=settings.stores.qdrant.url, prefer_grpc=False, timeout=10)
    try:
        stale = [c.name for c in (await client.get_collections()).collections]
        await drop_collections(client, *[n for n in stale if ns.owned_collection(n)])
    except Exception:  # pragma: no cover — a swept store is best-effort, never a test failure
        pass
    finally:
        await client.close()

    assert ns.REDIS_DB_INDEX != ns.PRODUCTION_REDIS_DB_INDEX
    redis = Redis.from_url(ns.REDIS_URL)
    try:
        await redis.flushdb()
    except Exception:  # pragma: no cover — same
        pass
    finally:
        await redis.aclose()


@pytest.fixture
def stack() -> None:
    """Marker fixture: depending on it documents "this test needs `make up`" without pulling in
    heavier per-backend fixtures a given test doesn't need. See MANUAL.md / BUILD_ORDER.md."""
    return None
