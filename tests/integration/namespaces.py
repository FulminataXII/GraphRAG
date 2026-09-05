"""Per-run namespaces for the integration suite. The ONE place a test-facing store name is
derived, and the reason `make test-int` can no longer destroy an ingested corpus.

**The incident this exists to make impossible.** After a `make test-int` run, Neo4j held zero
nodes and `graphrag.documents` zero rows, while Qdrant still held 129 chunk points and 161
entity points: `clean_neo4j` and `clean_pg` had wiped their (shared, production) stores around
every test that touched them, and nothing wiped Qdrant. The corpus was destroyed in two stores
and orphaned in the third, silently — a half-wiped corpus does not error, it just answers worse.

**The rule.** Isolation is by namespace, never by cleanup. A fixture that truncates a shared
store and restores it is still wrong: it fails open on a crashed run, and it leaves "did I just
destroy my corpus?" a question a human has to keep asking. Every name here carries `RUN_ID`, or
addresses a backend that production never addresses, so a fixture DROPPING its own namespace is
safe by construction:

    Qdrant      collections `test_<run>_<kind>[_<local>]`  (production: `chunks`, `entities`)
    Postgres    database    `graphrag_test_<run>`          (production: $POSTGRES_DB)
    Redis       db index    1..15                          (production: 0)
    Neo4j       a SEPARATE INSTANCE on port 7688           (production: 7687)

Neo4j is the odd one out because Community Edition supports exactly one user database —
`CREATE DATABASE x` answers "Unsupported administration command" — so there is no per-run
database to name and a per-run label prefix would mean templating every label in
`Neo4jGraphStore`'s Cypher (and its `_SCHEMA_STATEMENTS` static scan) for tests' benefit alone.
Instead `docker-compose.yml` declares a second `neo4j-test` service under the `test` profile,
which `make test-int` starts and `make up` does not. Port 7688 is the namespace.

`tests/integration/test_isolation_guard.py` asserts, from config rather than from a hardcoded
list, that every name here differs from what `config/base.yaml` + `.env` yield for production.
"""

from __future__ import annotations

import os
import uuid
from functools import cache
from pathlib import Path
from typing import Final

from pydantic import SecretStr

from graphrag.config.settings import Settings

_ENV_PATH: Final[Path] = Path(__file__).resolve().parents[2] / ".env"


def read_dotenv(key: str, default: str) -> str:
    """Read one key out of the repo `.env`.

    Postgres and Neo4j credentials live in `secrets`, which is populated ONLY from env/.env and
    has no YAML leaf to override — so a test that needs the value docker-compose actually
    authenticates with has to read this file, not `Settings`.
    """
    if not _ENV_PATH.is_file():
        return default
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return default


# Derived ONCE per run and exported, so a subprocess (or a future xdist worker) inherits the
# same value instead of minting a second namespace that nothing would ever clean up.
RUN_ID: Final[str] = os.environ.setdefault("GRAPHRAG_TEST_RUN_ID", uuid.uuid4().hex[:8])

# --- Neo4j: the separate instance, never 7687 ------------------------------------------------
NEO4J_TEST_BOLT_PORT: Final[int] = 7688
NEO4J_URI: Final[str] = f"bolt://localhost:{NEO4J_TEST_BOLT_PORT}"
NEO4J_PASSWORD: Final[str] = read_dotenv("GRAPHRAG_SECRETS__NEO4J_PASSWORD", "changeme")

# --- Postgres: a database this run creates and drops ------------------------------------------
POSTGRES_USER: Final[str] = read_dotenv("POSTGRES_USER", "graphrag")
POSTGRES_PASSWORD: Final[str] = read_dotenv("POSTGRES_PASSWORD", "changeme")
POSTGRES_HOST: Final[str] = "localhost:5432"
#: The database `make up`'s corpus lives in. Named here ONLY so nothing else may name it: the
#: maintenance connection that issues `CREATE DATABASE` has to connect somewhere, and the guard
#: test has to know what it must differ from.
PRODUCTION_POSTGRES_DB: Final[str] = read_dotenv("POSTGRES_DB", "graphrag")
POSTGRES_TEST_DB: Final[str] = f"graphrag_test_{RUN_ID}"


def postgres_dsn(database: str, *, driver: str = "postgresql+asyncpg") -> str:
    return f"{driver}://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}/{database}"


POSTGRES_DSN: Final[str] = postgres_dsn(POSTGRES_TEST_DB)
#: Connected to only to issue `CREATE DATABASE`/`DROP DATABASE`, which cannot run inside the
#: database they name. `postgres` is the server's own maintenance database and holds no app data.
POSTGRES_ADMIN_DSN: Final[str] = postgres_dsn("postgres")

# --- Redis: a logical db index, never 0 --------------------------------------------------------
#: Production (config/base.yaml `stores.redis.url`) is db 0 — the arq queue the running
#: `graphrag-worker-1` consumes AND the embedding/retrieval caches. An integration test that
#: enqueued there handed a real job to the real worker, and `flushdb()` wiped the real queue.
#: Redis ships 16 logical databases; 1..15 are unused by this system.
PRODUCTION_REDIS_DB_INDEX: Final[int] = 0
REDIS_DB_INDEX: Final[int] = int(RUN_ID, 16) % 15 + 1
REDIS_URL: Final[str] = f"redis://localhost:6379/{REDIS_DB_INDEX}"

# --- Qdrant: run-prefixed collections ----------------------------------------------------------
_COLLECTION_PREFIX: Final[str] = f"test_{RUN_ID}_"


def collection(kind: str, *, local: str = "") -> str:
    """A collection name no production process can be pointed at.

    `kind` is the role ("chunks", "entities"); `local` distinguishes collections WITHIN a run —
    several tests need a private pair (one deliberately creates a malformed collection), and a
    per-test suffix is what keeps them from colliding under the shared run prefix.
    """
    suffix = f"_{local}" if local else ""
    return f"{_COLLECTION_PREFIX}{kind}{suffix}"


if not RUN_ID or _COLLECTION_PREFIX in ("", "test_"):  # pragma: no cover — an import-time invariant
    raise RuntimeError(
        f"the per-run collection prefix degenerated to {_COLLECTION_PREFIX!r}; every name this "
        "module mints would then be a bare production name"
    )


@cache
def production_collections() -> frozenset[str]:
    """The Qdrant collections a NON-test process resolves to, read from the same config it
    reads. Not a hardcoded pair: renaming `retrieval.vector.collection` must move this too."""
    base = Settings()
    return frozenset({base.retrieval.vector.collection, base.resolution.collection})


def owned_collection(name: str) -> bool:
    """True only for names this module minted. The delete path checks it, so a fixture handed
    the wrong name deletes nothing instead of deleting the corpus.

    Two independent conditions, because one is not enough. A prefix test alone is only as strong
    as the prefix: with `_COLLECTION_PREFIX` degenerated to `""` this returns True for every
    name in the store, and the assertion that is supposed to refuse `chunks` waves it through —
    which is exactly how a deliberate sabotage of this module deleted the real `chunks` and
    `entities` collections while "proving" the guard worked. The import-time invariant above
    makes that particular degeneration impossible; the config lookup below makes the production
    names unusable no matter what the prefix says."""
    return name.startswith(_COLLECTION_PREFIX) and name not in production_collections()


def namespaced(base: Settings, *, local: str = "") -> Settings:
    """`base` with every store name/target swapped for this run's.

    Applied to a real `Settings()` (APP_ENV defaults to "local", so the store URLs already
    resolve to the published localhost ports) — this only moves the NAMES, so what a test
    exercises is the same configuration production runs with.
    """
    vector = base.retrieval.vector.model_copy(
        update={"collection": collection("chunks", local=local)}
    )
    return base.model_copy(
        update={
            "retrieval": base.retrieval.model_copy(update={"vector": vector}),
            "resolution": base.resolution.model_copy(
                update={"collection": collection("entities", local=local)}
            ),
            "stores": base.stores.model_copy(
                update={
                    "neo4j": base.stores.neo4j.model_copy(update={"uri": NEO4J_URI}),
                    "redis": base.stores.redis.model_copy(update={"url": REDIS_URL}),
                }
            ),
            # SecretStr, not str: `model_copy(update=...)` skips validation, so a bare str here
            # would blow up at the `.get_secret_value()` every consumer calls.
            "secrets": base.secrets.model_copy(update={"postgres_dsn": SecretStr(POSTGRES_DSN)}),
        }
    )
