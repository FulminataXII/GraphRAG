"""Alembic environment. Resolves the DSN through `graphrag.config.settings.get_settings()` —
the single source of truth for configuration (BLUEPRINT §5.1) — rather than reading an env var
directly here.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config

from graphrag.adapters.postgres.tables import SCHEMA, metadata
from graphrag.config.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def _dsn() -> str:
    settings = get_settings()
    dsn = settings.secrets.postgres_dsn.get_secret_value()
    # asyncpg dialect prefix; the DSN in .env uses the plain postgresql:// scheme shared with
    # Docker Compose / other consumers.
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return dsn


def run_migrations_offline() -> None:
    context.configure(
        url=_dsn(),
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: object) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _dsn()
    connectable = async_engine_from_config(configuration, prefix="sqlalchemy.")

    async with connectable.connect() as connection:
        # Alembic's own version table lives in the same dedicated schema as the app's tables
        # (see tables.py) — that schema must exist before either can be created in it.
        await connection.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        await connection.commit()
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
