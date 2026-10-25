"""Alembic 環境。

連線字串來源（依序）：程式傳入的 config.attributes["connection"]（測試用）、
alembic.ini 的 sqlalchemy.url（-x 或程式設定）、環境變數 DATABASE_URL。
支援同步（sqlite://、postgresql+psycopg）與非同步（postgresql+asyncpg）驅動；
離線模式（alembic upgrade head --sql）只產生 SQL，不連線。
"""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.db import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def _configure(connection: Connection | None = None, **kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=(connection is not None and connection.dialect.name == "sqlite"),
        connection=connection,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async(url: str) -> None:
    engine = async_engine_from_config({"sqlalchemy.url": url}, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_sync(connection)
        return

    url = _url()
    if make_url(url).get_dialect().is_async:
        asyncio.run(_run_async(url))
        return
    engine = engine_from_config({"sqlalchemy.url": url}, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as conn:
        _run_sync(conn)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
