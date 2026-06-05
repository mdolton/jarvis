"""Alembic environment — async, reads metadata from jarvis.persistence.models."""

import asyncio
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import models so MetaData has all tables registered.
from jarvis.persistence import models  # noqa: F401  (registers tables)
from jarvis.persistence.db import Base

config = context.config

# Allow tests / runtime to override the url via `-x db_url=...`
_x_args = context.get_x_argument(as_dictionary=True)
if "db_url" in _x_args:
    config.set_main_option("sqlalchemy.url", _x_args["db_url"])

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def _ensure_sqlite_parent_dir(url: str) -> None:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return
    if not parsed.database or parsed.database == ":memory:":
        return
    Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)


async def run_migrations_online() -> None:
    _ensure_sqlite_parent_dir(config.get_main_option("sqlalchemy.url"))
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
