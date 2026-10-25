"""SQLite 版工作佇列。

單一程序部署（§12）：同一使用者的序列化由 InProcessUserLock 負責，
這裡只需保證「一筆工作只被領一次」：領取用單一 UPDATE … RETURNING，在 BEGIN IMMEDIATE 交易內原子完成。
Worker 當掉後，租約（locked_until）過期的 running 工作會被重新領取。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import AsyncJob

from .jobs import ChatJob

CHAT = "chat"


class SQLiteJobSource:
    def __init__(
        self, engine: AsyncEngine, *, lease_seconds: float = 180.0,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._engine = engine
        self._lease = timedelta(seconds=lease_seconds)
        self._now = now

    async def claim(self, limit: int) -> list[ChatJob]:
        now = self._now()
        ready = (
            select(AsyncJob.id)
            .where(
                AsyncJob.type == CHAT,
                or_(
                    and_(AsyncJob.status == "queued", AsyncJob.available_at <= now),
                    and_(AsyncJob.status == "running", AsyncJob.locked_until < now),
                ),
            )
            .order_by(AsyncJob.received_at)
            .limit(limit)
            .scalar_subquery()
        )
        stmt = (
            update(AsyncJob)
            .where(AsyncJob.id.in_(ready))
            .values(status="running", attempts=AsyncJob.attempts + 1, locked_until=now + self._lease)
            .returning(AsyncJob.id, AsyncJob.user_id, AsyncJob.line_user_id, AsyncJob.message_text,
                       AsyncJob.received_at, AsyncJob.reply_token, AsyncJob.reply_deadline)
        )
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).all()
        jobs = [
            ChatJob(job_id=str(r.id), user_id=str(r.user_id), line_user_id=r.line_user_id,
                    message_text=r.message_text, received_at=r.received_at,
                    reply_token=r.reply_token, reply_deadline=r.reply_deadline)
            for r in rows
        ]
        return sorted(jobs, key=lambda j: j.received_at)  # RETURNING 不保證順序

    async def complete(self, job_id: str) -> None:
        await self._set(job_id, status="done", locked_until=None)

    async def fail(self, job_id: str, error: str) -> None:
        await self._set(job_id, status="failed", locked_until=None, last_error=error)

    async def release(self, job_id: str, delay_seconds: float) -> None:
        await self._set(job_id, status="queued", locked_until=None, attempts=AsyncJob.attempts - 1,
                        available_at=self._now() + timedelta(seconds=delay_seconds))

    async def _set(self, job_id: str, **values) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(update(AsyncJob).where(AsyncJob.id == uuid.UUID(job_id)).values(**values))
