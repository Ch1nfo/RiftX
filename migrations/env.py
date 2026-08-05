"""Alembic environment for RiftX's async SQLAlchemy database."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from riftx.persistence.capability_records import CapabilityRecord
from riftx.persistence.orm import Base

assert CapabilityRecord.__table__.metadata is Base.metadata

config = context.config
if config.config_file_name is not None:
    # Alembic can run in the Control Plane process during tests or managed
    # upgrade tooling. Do not disable already-imported RiftX loggers as a side
    # effect of applying the migration logging configuration.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

configured_url = os.getenv("RIFTX_DATABASE_URL")
if configured_url:
    config.set_main_option("sqlalchemy.url", configured_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def configure_and_run_migrations(connection: object) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(configure_and_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
