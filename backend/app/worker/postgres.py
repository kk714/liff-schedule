"""PostgreSQL 版工作佇列與使用者 advisory lock。

D8：後端測試使用 SQLite，這裡的 SQL（FOR UPDATE SKIP LOCKED、pg_try_advisory_lock）
無法在 CI 驗證，必須在 PostgreSQL 測試／預備環境驗收（設計文件 §14.2、T4）。

async_jobs 的定義在 app/db/models.py（AsyncJob）；tests/db/test_schema.py 會檢查這裡用到的欄位都存在。
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .jobs import ChatJob

_CLAIM_SQL = text(
    """
    UPDATE async_jobs AS j
       SET status = 'running',
           attempts = j.attempts + 1,
           locked_until = now() + make_interval(secs => :lease_seconds)
      FROM (
            SELECT id FROM async_jobs
             WHERE type = 'chat'
               AND (
                    (status = 'queued' AND available_at <= now())
                 OR (status = 'running' AND locked_until < now())  -- Worker 當掉，租約過期後重領
               )
             ORDER BY received_at
             LIMIT :limit
             FOR UPDATE SKIP LOCKED
           ) AS picked
     WHERE j.id = picked.id
 RETURNING j.id, j.user_id, j.line_user_id, j.message_text, j.received_at,
           j.reply_token, j.reply_deadline
    """
)
_COMPLETE_SQL = text("UPDATE async_jobs SET status = 'done', locked_until = NULL WHERE id = :id")
_FAIL_SQL = text(
    "UPDATE async_jobs SET status = 'failed', locked_until = NULL, last_error = :error WHERE id = :id"
)
_RELEASE_SQL = text(
    """
    UPDATE async_jobs
       SET status = 'queued', locked_until = NULL, attempts = attempts - 1,
           available_at = now() + make_interval(secs => :delay)
     WHERE id = :id
    """
)


class PostgresJobSource:
    def __init__(self, engine: AsyncEngine, *, lease_seconds: float = 180.0) -> None:
        self._engine = engine
        self._lease_seconds = lease_seconds

    async def claim(self, limit: int) -> list[ChatJob]:
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(_CLAIM_SQL, {"limit": limit, "lease_seconds": self._lease_seconds})
            ).mappings().all()
        jobs = [
            ChatJob(
                job_id=str(r["id"]),
                user_id=str(r["user_id"]),
                line_user_id=r["line_user_id"],
                message_text=r["message_text"],
                received_at=r["received_at"],
                reply_token=r["reply_token"],
                reply_deadline=r["reply_deadline"],
            )
            for r in rows
        ]
        return sorted(jobs, key=lambda j: j.received_at)  # RETURNING 不保證順序

    async def complete(self, job_id: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(_COMPLETE_SQL, {"id": job_id})

    async def fail(self, job_id: str, error: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(_FAIL_SQL, {"id": job_id, "error": error})

    async def release(self, job_id: str, delay_seconds: float) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(_RELEASE_SQL, {"id": job_id, "delay": delay_seconds})


def advisory_key(user_id: str) -> int:
    """把使用者 UUID 轉成 pg advisory lock 用的 signed 64-bit 鍵。"""
    return int.from_bytes(uuid.UUID(user_id).bytes[:8], "big", signed=True)


class PostgresAdvisoryLock:
    """Session 級 advisory lock，每把鎖占用一條專屬連線；程序當掉時連線中斷即自動釋放。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def try_acquire(self, user_id: str):
        key = advisory_key(user_id)
        conn = await self._engine.connect()
        try:
            acquired = (
                await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
            ).scalar_one()
        except BaseException:
            await conn.close()
            raise
        if not acquired:
            await conn.close()
            return None

        @asynccontextmanager
        async def held():
            try:
                yield
            finally:
                try:
                    await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                finally:
                    await conn.close()

        return held()
