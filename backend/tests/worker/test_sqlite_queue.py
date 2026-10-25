from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db import Base
from app.db.engine import create_engine, session_factory
from app.db.models import AsyncJob, Organization, User
from app.worker import HandlerResult, Worker, WorkerSettings
from app.worker.memory import InProcessUserLock
from app.worker.sqlite_queue import SQLiteJobSource

T0 = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
async def setup(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'q.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_factory(engine)() as s:
        org = Organization(name="公司")
        s.add(org)
        await s.flush()
        users = [User(organization_id=org.id, display_name=f"u{i}") for i in range(2)]
        s.add_all(users)
        await s.commit()
    clock = Clock()
    yield engine, SQLiteJobSource(engine, lease_seconds=60, now=clock), users, clock
    await engine.dispose()


async def enqueue(engine, user, text, offset_ms, *, token=True):
    async with session_factory(engine)() as s:
        job = AsyncJob(type="chat", user_id=user.id, line_user_id="U1", message_text=text,
                       received_at=T0 + timedelta(milliseconds=offset_ms), available_at=T0,
                       reply_token="rt" if token else None,
                       reply_deadline=T0 + timedelta(seconds=30) if token else None)
        s.add(job)
        await s.commit()
        return str(job.id)


async def status(engine, job_id):
    async with session_factory(engine)() as s:
        return await s.get(AsyncJob, uuid.UUID(job_id))


async def test_claim_in_received_order_and_only_once(setup):
    engine, source, users, _ = setup
    ids = [await enqueue(engine, users[0], f"m{i}", offset) for i, offset in enumerate([30, 10, 20])]

    first, second = await asyncio.gather(source.claim(2), source.claim(2))

    claimed = [j.job_id for j in first + second]
    assert sorted(claimed) == sorted(ids)  # 三筆各被領一次
    assert len(set(claimed)) == 3
    assert [j.message_text for j in sorted(first + second, key=lambda j: j.received_at)] == ["m1", "m2", "m0"]
    job = next(j for j in first + second if j.job_id == ids[0])
    assert job.reply_deadline == T0 + timedelta(seconds=30)


async def test_complete_fail_release(setup):
    engine, source, users, clock = setup
    a, b, c = [await enqueue(engine, users[0], t, i) for i, t in enumerate("abc")]
    await source.claim(3)

    await source.complete(a)
    await source.fail(b, "boom")
    await source.release(c, 5)

    assert (await status(engine, a)).status == "done"
    assert ((await status(engine, b)).status, (await status(engine, b)).last_error) == ("failed", "boom")
    released = await status(engine, c)
    assert (released.status, released.attempts) == ("queued", 0)
    assert await source.claim(5) == []
    clock.t += timedelta(seconds=5)
    assert [j.job_id for j in await source.claim(5)] == [c]


async def test_expired_lease_is_reclaimed(setup):
    engine, source, users, clock = setup
    job_id = await enqueue(engine, users[0], "x", 0)
    await source.claim(1)
    assert await source.claim(1) == []
    clock.t += timedelta(seconds=61)  # Worker 當掉，租約過期
    reclaimed = await source.claim(1)
    assert [j.job_id for j in reclaimed] == [job_id]
    assert (await status(engine, job_id)).attempts == 2


class EchoHandler:
    def __init__(self):
        self.seen = []

    async def handle(self, job, *, deadline):
        self.seen.append(job.message_text)
        await asyncio.sleep(0.01)
        return HandlerResult([{"type": "text", "text": job.message_text}])


class NullMessenger:
    async def start_loading(self, *a): ...
    async def reply(self, *a): ...
    async def push(self, *a, retry_key): ...


async def test_worker_end_to_end_on_sqlite(setup):
    engine, source, users, clock = setup
    for i in range(3):
        await enqueue(engine, users[0], f"u0-{i}", i)
    await enqueue(engine, users[1], "u1-0", 0)
    handler = EchoHandler()
    worker = Worker(source, InProcessUserLock(), handler, NullMessenger(),
                    WorkerSettings(concurrency=2, lock_busy_retry_seconds=0), now=clock)

    for _ in range(20):
        await worker.run_once()
        await worker.drain()

    assert [m for m in handler.seen if m.startswith("u0")] == ["u0-0", "u0-1", "u0-2"]  # 同一使用者依序
    async with session_factory(engine)() as s:
        statuses = (await s.scalars(select(AsyncJob.status))).all()
    assert statuses == ["done"] * 4
