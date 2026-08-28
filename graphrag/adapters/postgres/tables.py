"""SQLAlchemy Core table definitions (no ORM). See BLUEPRINT §5.4.

Six tables, one shared `metadata`. Only `documents` and `chunk_sources` have readers/writers in
this build order (`ledger.py`, `sources.py`). `jobs`, `api_keys`, `eval_runs`, `eval_results` are
schema stubs for BO-11/BO-12 — defining the full shape now (rather than one Alembic revision per
later BO) is what BLUEPRINT §5.4 asks tables.py to do; their column shapes will be revisited by
the BO that first reads/writes them.

`chunk_sources` has `PRIMARY KEY (chunk_id, doc_id)` — this is the concurrency arbiter for
multi-source retention (BLUEPRINT §5.4, ARCHITECTURE §1.3): an `INSERT ... ON CONFLICT DO NOTHING`
against this key is safe under unlimited worker concurrency, no lock, no shard.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

# A dedicated schema, not `public`: this Postgres instance is shared with Phoenix and LiteLLM
# (ARCHITECTURE §4.2 — "you already run Postgres, point Phoenix at it"), both of which migrate
# their own tables into `public` of the same database. Phoenix's own schema already includes a
# generically-named `api_keys` table; a bare `public.api_keys` here would collide with it.
SCHEMA = "graphrag"

metadata = MetaData(schema=SCHEMA)

documents = Table(
    "documents",
    metadata,
    Column("doc_id", String(36), primary_key=True),
    Column("uri", Text, nullable=False),
    Column("sha256", String(64), nullable=False, unique=True),
    Column("mime_type", String(255), nullable=False),
    Column("status", String(32), nullable=False),
    Column("error_code", String(64), nullable=True),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("corpus_version", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# The single global corpus version counter. One row (id=1), updated with
# `UPDATE ... SET value = value + 1 RETURNING value` for atomicity under concurrency.
corpus_version_counter = Table(
    "corpus_version_counter",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("value", Integer, nullable=False, server_default="0"),
)

chunk_sources = Table(
    "chunk_sources",
    metadata,
    Column("chunk_id", UUID(as_uuid=True), primary_key=True),
    Column("doc_id", String(36), primary_key=True),
    Column("uri", Text, nullable=False),
    Column("page", Integer, nullable=True),
    Column("char_start", Integer, nullable=False),
    Column("char_end", Integer, nullable=False),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
)

# --- Schema stubs below: no reader/writer until the BO named in the comment lands. ---

jobs = Table(
    "jobs",
    metadata,
    Column("job_id", String(255), primary_key=True),
    Column("task", String(255), nullable=False),
    Column("state", String(32), nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("correlation_id", String(64), nullable=False),
    Column("enqueued_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("error", Text, nullable=True),
)

api_keys = Table(
    "api_keys",
    metadata,
    Column("key_id", String(36), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("key_hash", Text, nullable=False),
    Column("role", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
)

eval_runs = Table(
    "eval_runs",
    metadata,
    Column("run_id", String(36), primary_key=True),
    Column("git_sha", String(40), nullable=False),
    Column("config_hash", String(64), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("metrics", JSONB, nullable=True),
)

eval_results = Table(
    "eval_results",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(36), nullable=False),
    Column("item_id", String(255), nullable=False),
    Column("result", JSONB, nullable=False),
)
