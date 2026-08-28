"""BO-03: initial schema — documents, chunk_sources, corpus_version_counter, and the
schema stubs for jobs/api_keys/eval_runs/eval_results owned by later build orders.

All tables live in the `graphrag` schema, not `public`: this Postgres instance is shared with
Phoenix and LiteLLM (ARCHITECTURE §4.2), both of which migrate their own tables into `public` —
Phoenix's schema already includes a generically-named `api_keys` table, which a bare
`public.api_keys` here would collide with. `env.py` creates the schema before this migration
runs.

Revision ID: 0001_bo03_initial
Revises:
Create Date: 2026-08-28

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from graphrag.adapters.postgres.tables import SCHEMA

# revision identifiers, used by Alembic.
revision: str = "0001_bo03_initial"
down_revision: str | None = None
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("doc_id", sa.String(36), primary_key=True),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("corpus_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )

    op.create_table(
        "corpus_version_counter",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("value", sa.Integer(), nullable=False, server_default="0"),
        schema=SCHEMA,
    )
    op.execute(f"INSERT INTO {SCHEMA}.corpus_version_counter (id, value) VALUES (1, 0)")

    op.create_table(
        "chunk_sources",
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("doc_id", sa.String(36), primary_key=True),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )

    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(255), primary_key=True),
        sa.Column("task", sa.String(255), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        schema=SCHEMA,
    )

    op.create_table(
        "api_keys",
        sa.Column("key_id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )

    op.create_table(
        "eval_runs",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("git_sha", sa.String(40), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )

    op.create_table(
        "eval_results",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("item_id", sa.String(255), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("eval_results", schema=SCHEMA)
    op.drop_table("eval_runs", schema=SCHEMA)
    op.drop_table("api_keys", schema=SCHEMA)
    op.drop_table("jobs", schema=SCHEMA)
    op.drop_table("chunk_sources", schema=SCHEMA)
    op.drop_table("corpus_version_counter", schema=SCHEMA)
    op.drop_table("documents", schema=SCHEMA)
