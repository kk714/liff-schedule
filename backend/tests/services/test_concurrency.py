"""兩條連線同時操作同一筆資料（BEGIN IMMEDIATE 序列化寫入，取代列鎖）。"""

from __future__ import annotations

import asyncio
from datetime import datetime

from sqlalchemy import func, select

from app.db.engine import session_factory
from app.db.models import Event
from app.llm.schemas import ParsedEvent, ParsedSchedule
from app.services import DraftFields, DraftInput, DraftService, draft_input_from_parsed
from app.services.errors import DraftVersionConflict, EventVersionConflict

from .conftest import NOW

svc = DraftService()


async def _confirm(engine, user, draft_id, version):
    async with session_factory(engine)() as s:
        try:
            result = await svc.confirm(s, user, draft_id, version, now=NOW)
            await s.commit()
            return result
        except Exception as exc:
            await s.rollback()
            return exc


async def test_a06_concurrent_double_confirm_creates_one_event(engine, session, world):
    parsed = ParsedSchedule(intent="create", event=ParsedEvent(title="國網中心拜訪",
                                                               start_local=datetime(2026, 11, 1, 14)))
    draft = await svc.create(session, world.me, draft_input_from_parsed(parsed), now=NOW)
    await session.commit()  # 釋放寫入鎖，讓兩條連線競爭

    results = await asyncio.gather(*(_confirm(engine, world.me, draft.id, 1) for _ in range(2)))

    assert sorted(r.already_committed for r in results) == [False, True]
    assert results[0].event.id == results[1].event.id
    async with session_factory(engine)() as s:
        assert await s.scalar(select(func.count()).select_from(Event)) == 1


async def test_a07_concurrent_edits_of_same_event_one_wins(engine, session, world):
    parsed = ParsedSchedule(intent="create", event=ParsedEvent(title="週會", start_local=datetime(2026, 11, 2, 10)))
    created = await svc.create(session, world.me, draft_input_from_parsed(parsed), now=NOW)
    event = (await svc.confirm(session, world.me, created.id, 1, now=NOW)).event
    a = await svc.create(session, world.me, DraftInput("update", DraftFields(title="A"), event_id=event.id), now=NOW)
    b = await svc.create(session, world.me, DraftInput("update", DraftFields(title="B"), event_id=event.id), now=NOW)
    await session.commit()

    results = await asyncio.gather(_confirm(engine, world.me, a.id, 1), _confirm(engine, world.me, b.id, 1))

    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 1 and isinstance(errors[0], (EventVersionConflict, DraftVersionConflict))
    async with session_factory(engine)() as s:
        stored = await s.get(Event, event.id)
        assert stored.version == 2 and stored.title in {"A", "B"}
