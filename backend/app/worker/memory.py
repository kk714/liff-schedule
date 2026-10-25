"""單一程序用的佇列與使用者鎖：供測試與本機開發，不可用於多程序正式環境。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .jobs import ChatJob


@dataclass
class _Entry:
    job: ChatJob
    status: str = "queued"  # queued | running | done | failed
    available_at: datetime | None = None
    attempts: int = 0
    last_error: str | None = None


class MemoryJobSource:
    def __init__(self, *, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._entries: dict[str, _Entry] = {}
        self._now = now

    def enqueue(self, job: ChatJob) -> None:
        self._entries[job.job_id] = _Entry(job)

    def status(self, job_id: str) -> str:
        return self._entries[job_id].status

    def entry(self, job_id: str) -> _Entry:
        return self._entries[job_id]

    async def claim(self, limit: int) -> list[ChatJob]:
        now = self._now()
        ready = sorted(
            (e for e in self._entries.values()
             if e.status == "queued" and (e.available_at is None or e.available_at <= now)),
            key=lambda e: e.job.received_at,
        )[:limit]
        for e in ready:
            e.status = "running"
            e.attempts += 1
        return [e.job for e in ready]

    async def complete(self, job_id: str) -> None:
        self._entries[job_id].status = "done"

    async def fail(self, job_id: str, error: str) -> None:
        entry = self._entries[job_id]
        entry.status, entry.last_error = "failed", error

    async def release(self, job_id: str, delay_seconds: float) -> None:
        entry = self._entries[job_id]
        entry.status = "queued"
        entry.attempts -= 1
        entry.available_at = self._now() + timedelta(seconds=delay_seconds)


class InProcessUserLock:
    """每位使用者一把 asyncio.Lock；被占用時 try_acquire 回傳 None。"""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    async def try_acquire(self, user_id: str):
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            return None
        await lock.acquire()

        @asynccontextmanager
        async def held():
            try:
                yield
            finally:
                lock.release()

        return held()
