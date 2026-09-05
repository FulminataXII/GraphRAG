"""The guard that makes the isolation stick: no integration fixture may resolve to a store
production uses. See `tests/integration/namespaces.py` for the incident behind it.

Everything here is derived from configuration — `config/base.yaml` + `config/local.yaml` through
`Settings()`, and `.env` for the secrets that have no YAML leaf — never from a hardcoded list of
names. A list would have to be updated by the same person who just renamed something, which is
precisely when nobody remembers to.

Three layers, because "different names" alone is not the requirement:

  1. every namespace the test config yields differs from the one production's config yields;
  2. the live fixtures actually connect to those namespaces (a correct constant that no fixture
     reads would prove nothing);
  3. the destructive helpers REFUSE a production name when handed one directly, so a future
     fixture that resolves wrongly fails loudly instead of deleting a corpus.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest
from neo4j import AsyncDriver, AsyncGraphDatabase
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from graphrag.config.settings import Settings
from tests.integration import namespaces as ns
from tests.integration.conftest import drop_collections

pytestmark = pytest.mark.integration


@pytest.fixture
def production() -> Settings:
    """What every non-test process on this host resolves to. No `set_required_secrets` here on
    purpose: the real `.env` is what supplies `secrets.postgres_dsn`, and the production
    database name is exactly what the test database must differ from."""
    return Settings()


@pytest.fixture
def isolated(production: Settings) -> Settings:
    return ns.namespaced(production)


def _redis_db(url: str) -> int:
    return int(urlparse(url).path.lstrip("/") or 0)


def _pg_database(dsn: str) -> str:
    return urlparse(dsn).path.lstrip("/")


# ---------------------------------------------------------------------------
# 1. the names differ from production's
# ---------------------------------------------------------------------------
def test_qdrant_collections_are_not_production(isolated: Settings, production: Settings) -> None:
    assert isolated.retrieval.vector.collection != production.retrieval.vector.collection
    assert isolated.resolution.collection != production.resolution.collection
    assert ns.owned_collection(isolated.retrieval.vector.collection)
    assert ns.owned_collection(isolated.resolution.collection)
    # And production's names are NOT ones the delete path would accept.
    assert not ns.owned_collection(production.retrieval.vector.collection)
    assert not ns.owned_collection(production.resolution.collection)


def test_neo4j_target_is_not_production(isolated: Settings, production: Settings) -> None:
    """Community Edition has one database, so the namespace is the instance. `database` is
    still `neo4j` on both sides — asserting on the port is asserting on the thing that
    actually differs."""
    isolated_uri = urlparse(isolated.stores.neo4j.uri)
    production_uri = urlparse(production.stores.neo4j.uri)
    assert isolated_uri.port != production_uri.port
    assert isolated_uri.port == ns.NEO4J_TEST_BOLT_PORT


def test_redis_db_index_is_not_production(isolated: Settings, production: Settings) -> None:
    assert _redis_db(isolated.stores.redis.url) != _redis_db(production.stores.redis.url)
    assert _redis_db(isolated.stores.redis.url) != ns.PRODUCTION_REDIS_DB_INDEX
    assert 1 <= _redis_db(isolated.stores.redis.url) <= 15


def test_postgres_database_is_not_production(isolated: Settings, production: Settings) -> None:
    isolated_db = _pg_database(isolated.secrets.postgres_dsn.get_secret_value())
    production_db = _pg_database(production.secrets.postgres_dsn.get_secret_value())
    assert isolated_db != production_db
    assert isolated_db == ns.POSTGRES_TEST_DB
    # The `.env`-derived name the fixtures compare against must be the one production really
    # uses, or the comparison above is vacuous.
    assert production_db == ns.PRODUCTION_POSTGRES_DB


# ---------------------------------------------------------------------------
# 2. the live fixtures connect to those namespaces
# ---------------------------------------------------------------------------
async def test_pg_engine_is_the_per_run_database(clean_pg: AsyncEngine) -> None:
    async with clean_pg.connect() as conn:
        current = (await conn.execute(text("SELECT current_database()"))).scalar_one()
    assert current == ns.POSTGRES_TEST_DB
    assert current != ns.PRODUCTION_POSTGRES_DB


async def test_redis_client_is_not_db_zero(redis_client: Redis) -> None:
    db_index = redis_client.connection_pool.connection_kwargs.get("db", 0)
    assert db_index == ns.REDIS_DB_INDEX
    assert db_index != ns.PRODUCTION_REDIS_DB_INDEX


async def test_neo4j_writes_cannot_reach_the_production_instance(
    clean_neo4j: AsyncDriver, production: Settings
) -> None:
    """Writes through the fixture, then looks for that write on the production instance.

    Asserting on the URI string would only prove the fixture was BUILT correctly; asserting on
    the address the server reports is useless here, because Neo4j advertises its in-container
    `localhost:7687` on both instances. So this does the only thing that actually settles it:
    creates a node carrying this run's id, and confirms the production graph never sees it. The
    production side is strictly read-only — a MATCH and a count, nothing else."""
    await clean_neo4j.execute_query(
        "CREATE (:IsolationProbe {run_id: $run_id})", run_id=ns.RUN_ID, database_="neo4j"
    )
    records, _summary, _keys = await clean_neo4j.execute_query(
        "MATCH (n:IsolationProbe {run_id: $run_id}) RETURN count(n) AS c",
        run_id=ns.RUN_ID,
        database_="neo4j",
    )
    assert records[0]["c"] == 1, "the probe was not written to the instance under test"

    assert production.stores.neo4j.uri != ns.NEO4J_URI
    production_driver = AsyncGraphDatabase.driver(
        production.stores.neo4j.uri, auth=("neo4j", ns.NEO4J_PASSWORD)
    )
    try:
        records, _summary, _keys = await production_driver.execute_query(
            "MATCH (n:IsolationProbe) RETURN count(n) AS c",
            database_=production.stores.neo4j.database,
        )
    finally:
        await production_driver.close()
    assert records[0]["c"] == 0, (
        "an integration-fixture write landed on the production Neo4j instance"
    )


async def test_no_production_collection_exists_under_the_run_prefix(
    production: Settings,
) -> None:
    """Whatever this run creates in Qdrant, `chunks`/`entities` are not among the names it can
    create — so a crashed run leaves orphans under `test_<run>_`, never a half-written corpus."""
    client = AsyncQdrantClient(url=production.stores.qdrant.url, prefer_grpc=False, timeout=10)
    try:
        existing = {c.name for c in (await client.get_collections()).collections}
    finally:
        await client.close()
    minted = {ns.collection("chunks"), ns.collection("entities")}
    assert minted.isdisjoint(
        {production.retrieval.vector.collection, production.resolution.collection}
    )
    # Production's collections may or may not exist on this machine; if they do, they must not
    # be ones this run would consider its own to drop.
    for name in existing & {
        production.retrieval.vector.collection,
        production.resolution.collection,
    }:
        assert not ns.owned_collection(name)


# ---------------------------------------------------------------------------
# 3. the destructive helpers refuse a production name
# ---------------------------------------------------------------------------
async def test_drop_collections_refuses_a_production_name(production: Settings) -> None:
    client = AsyncQdrantClient(url=production.stores.qdrant.url, prefer_grpc=False, timeout=10)
    try:
        with pytest.raises(AssertionError, match="not a collection this run created"):
            await drop_collections(client, production.retrieval.vector.collection)
    finally:
        await client.close()
