"""The integration suite's destructive guards, exercised with nothing connected.

`tests/integration/namespaces.py` is what stops `make test-int` from reaching a real store, and
its `check_*` functions are the preconditions the destructive fixtures call before dropping a
database, flushing a Redis db, deleting a Qdrant collection, or wiping a graph.

This verifies them the only way a guard like that should ever be verified: by handing each one a
value fabricated to look like production and asserting it refuses. There is no client, no
driver, no engine and no container in this module — the earlier attempt to prove the same
property by pointing a live fixture at the production name is what deleted the real `chunks` and
`entities` collections, because a sabotage severe enough to make the guard fail is a sabotage
severe enough to disable it. A unit test cannot make that mistake.

The production values are read from configuration, never hardcoded: `production_settings()`
resolves `config/base.yaml` + `config/local.yaml`, and `PRODUCTION_POSTGRES_DB` comes from the
`.env` key that has no YAML leaf. Rename a collection in `base.yaml` and these move with it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import pytest

from graphrag.config.settings import Settings
from tests.integration import namespaces as ns
from tests.integration.conftest import drop_collections
from tests.unit._settings_helpers import set_required_secrets


@pytest.fixture(autouse=True)
def _production_config(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """`namespaces.production_settings()` is cached, and the autouse unit-test fixture strips
    every GRAPHRAG_* var — so prime the cache with a `Settings` built under stubbed secrets, and
    clear it afterwards so an integration run in the same process never inherits it."""
    set_required_secrets(monkeypatch)
    # Hold the cached callable itself: a test may monkeypatch the module attribute, and this
    # fixture's teardown runs before monkeypatch's undo does.
    cached = ns.production_settings
    cached.cache_clear()
    settings = cached()
    yield settings
    cached.cache_clear()


class _ExplodingQdrantClient:
    """Records nothing and deletes nothing: any call at all is the failure this asserts against."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_collection(self, name: str) -> Any:  # pragma: no cover — must not be reached
        self.deleted.append(name)
        raise AssertionError(f"delete_collection({name!r}) was reached; the guard did not refuse")


# ---------------------------------------------------------------------------
# qdrant
# ---------------------------------------------------------------------------
def test_check_collection_refuses_every_production_collection(_production_config: Settings) -> None:
    for name in (
        _production_config.retrieval.vector.collection,
        _production_config.resolution.collection,
    ):
        with pytest.raises(ns.ProductionNamespaceError, match="not a collection this run created"):
            ns.check_collection(name)


def test_check_collection_allows_this_runs_own_names() -> None:
    ns.check_collection(ns.collection("chunks"))
    ns.check_collection(ns.collection("entities", local="abcd1234"))


def test_check_collection_refuses_a_name_that_only_looks_namespaced() -> None:
    """The prefix is not a password. A collection some other tool left behind under a
    similar-looking name is still not this run's to delete."""
    with pytest.raises(ns.ProductionNamespaceError):
        ns.check_collection("test_someoneelse_chunks")


async def test_drop_collections_refuses_before_touching_the_client(
    _production_config: Settings,
) -> None:
    """The precondition runs BEFORE the delete, not around it — the ordering is the whole
    point, so assert on it with a client that fails if it is ever called."""
    client = _ExplodingQdrantClient()
    with pytest.raises(ns.ProductionNamespaceError):
        await drop_collections(client, _production_config.retrieval.vector.collection)  # type: ignore[arg-type]
    assert client.deleted == []


async def test_drop_collections_refuses_the_whole_batch_on_one_bad_name(
    _production_config: Settings,
) -> None:
    """A production name anywhere in the list stops the batch. Deleting the safe ones first and
    failing afterwards would make a half-applied teardown look like a passing guard."""
    client = _ExplodingQdrantClient()
    with pytest.raises(ns.ProductionNamespaceError):
        await drop_collections(
            client,  # type: ignore[arg-type]
            _production_config.resolution.collection,
            ns.collection("chunks"),
        )
    assert client.deleted == []


# ---------------------------------------------------------------------------
# postgres
# ---------------------------------------------------------------------------
def test_check_database_refuses_the_production_database() -> None:
    with pytest.raises(ns.ProductionNamespaceError, match="refusing to create or drop database"):
        ns.check_database(ns.PRODUCTION_POSTGRES_DB)


def test_check_database_refuses_anything_not_minted_here() -> None:
    """Not just `!= production`: a name this module did not mint is not this session's to drop
    either, so `postgres`, `template1` and a neighbouring project's database are all refused."""
    for name in ("postgres", "template1", "graphrag_prod", "graphrag_test"):
        with pytest.raises(ns.ProductionNamespaceError):
            ns.check_database(name)


def test_check_database_allows_this_runs_own_database() -> None:
    ns.check_database(ns.POSTGRES_TEST_DB)


# ---------------------------------------------------------------------------
# redis
# ---------------------------------------------------------------------------
def test_check_redis_db_refuses_the_production_index(_production_config: Settings) -> None:
    production_index = int(urlparse(_production_config.stores.redis.url).path.lstrip("/") or 0)
    with pytest.raises(ns.ProductionNamespaceError, match="refusing to flush redis db"):
        ns.check_redis_db(production_index)


def test_check_redis_db_refuses_db_zero_even_if_config_moves(
    monkeypatch: pytest.MonkeyPatch, _production_config: Settings
) -> None:
    """db 0 is where the arq queue the worker consumes lives. If someone points
    `stores.redis.url` at another index, db 0 must STILL be refused — the queue does not move
    just because the config did."""
    moved = _production_config.model_copy(
        update={
            "stores": _production_config.stores.model_copy(
                update={
                    "redis": _production_config.stores.redis.model_copy(
                        update={"url": "redis://localhost:6379/7"}
                    )
                }
            )
        }
    )
    monkeypatch.setattr(ns, "production_settings", lambda: moved)
    with pytest.raises(ns.ProductionNamespaceError):
        ns.check_redis_db(0)
    with pytest.raises(ns.ProductionNamespaceError):
        ns.check_redis_db(7)


def test_check_redis_db_allows_this_runs_own_index() -> None:
    ns.check_redis_db(ns.REDIS_DB_INDEX)
    assert 1 <= ns.REDIS_DB_INDEX <= 15


# ---------------------------------------------------------------------------
# neo4j
# ---------------------------------------------------------------------------
def test_check_neo4j_uri_refuses_the_production_instance(_production_config: Settings) -> None:
    with pytest.raises(ns.ProductionNamespaceError, match="production Neo4j instance"):
        ns.check_neo4j_uri(_production_config.stores.neo4j.uri)


def test_check_neo4j_uri_refuses_the_production_port_under_any_host(
    _production_config: Settings,
) -> None:
    """Community Edition has one database, so the port is the entire namespace. A URI that
    reaches 7687 by another spelling is the same instance."""
    port = urlparse(_production_config.stores.neo4j.uri).port
    for uri in (f"bolt://127.0.0.1:{port}", f"neo4j://localhost:{port}"):
        with pytest.raises(ns.ProductionNamespaceError):
            ns.check_neo4j_uri(uri)


def test_check_neo4j_uri_allows_this_runs_own_instance() -> None:
    ns.check_neo4j_uri(ns.NEO4J_URI)
    assert ns.NEO4J_TEST_BOLT_PORT == 7688


# ---------------------------------------------------------------------------
# the derivation itself
# ---------------------------------------------------------------------------
def test_namespaced_settings_differ_from_production_everywhere(
    _production_config: Settings,
) -> None:
    """The unit-level counterpart of `tests/integration/test_isolation_guard.py`'s first layer:
    same assertions, no containers, so `make test` catches a regression too."""
    isolated = ns.namespaced(_production_config)

    assert isolated.retrieval.vector.collection != _production_config.retrieval.vector.collection
    assert isolated.resolution.collection != _production_config.resolution.collection
    assert (
        urlparse(isolated.stores.neo4j.uri).port
        != urlparse(_production_config.stores.neo4j.uri).port
    )
    assert (
        urlparse(isolated.stores.redis.url).path
        != urlparse(_production_config.stores.redis.url).path
    )
    assert (
        urlparse(isolated.secrets.postgres_dsn.get_secret_value()).path.lstrip("/")
        == ns.POSTGRES_TEST_DB
    )
    assert ns.POSTGRES_TEST_DB != ns.PRODUCTION_POSTGRES_DB


def test_every_namespaced_value_passes_its_own_guard(_production_config: Settings) -> None:
    """A namespace the derivation produces must be one the guards accept, or `make test-int`
    refuses to run at all. Both directions matter: refusing production is useless if the guard
    also refuses the run's own stores."""
    isolated = ns.namespaced(_production_config)
    ns.check_collection(isolated.retrieval.vector.collection)
    ns.check_collection(isolated.resolution.collection)
    ns.check_neo4j_uri(isolated.stores.neo4j.uri)
    ns.check_redis_db(int(urlparse(isolated.stores.redis.url).path.lstrip("/")))
    ns.check_database(urlparse(isolated.secrets.postgres_dsn.get_secret_value()).path.lstrip("/"))
