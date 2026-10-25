"""SQLite 連線設定（單一檔案資料庫，隨服務部署；設計文件 §12）。

- WAL：讀取不會被寫入擋住。
- busy_timeout：寫入鎖被占用時等待，而不是立刻失敗。
- foreign_keys：SQLite 預設關閉外鍵檢查，必須每條連線開啟。
- 每個交易以 BEGIN IMMEDIATE 開始：一開始就取得寫入鎖，
  寫入交易彼此序列化。草稿確認「讀取→檢查→寫入」因此不會互相穿插（取代 PostgreSQL 的列鎖）。
"""

from __future__ import annotations

import os

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

DEFAULT_URL = "sqlite+aiosqlite:///./data/app.db"
BUSY_TIMEOUT_MS = 5000


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def configure_sqlite(sync_engine) -> None:
    @event.listens_for(sync_engine, "connect")
    def _on_connect(dbapi_connection, _record):
        dbapi_connection.isolation_level = None  # 交易由下方 begin 事件自行開始
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    @event.listens_for(sync_engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")


def create_engine(url: str | None = None) -> AsyncEngine:
    engine = create_async_engine(url or database_url())
    if engine.dialect.name == "sqlite":
        configure_sqlite(engine.sync_engine)
    return engine


def session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
