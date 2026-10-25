"""草稿確認交易（§5、A02、A06、A07、A15）。並行情境的列鎖需 PostgreSQL 環境驗收（§14.2）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.db.models import AuditLog, Event
from app.llm.schemas import ParsedEvent, ParsedSchedule
from app.services import DraftFields, DraftInput, DraftService, draft_input_from_parsed
from app.services.errors import (
    DraftGone,
    DraftVersionConflict,
    EventVersionConflict,
    InvalidDraft,
    MemberInactive,
    NotFound,
)

from .conftest import NOW

svc = DraftService()


def parsed_create(**event) -> ParsedSchedule:
    base = {"title": "國網中心拜訪", "start_local": datetime(2026, 11, 1, 14, 0)}
    return ParsedSchedule(intent="create", event=ParsedEvent(**{**base, **event}))


async def confirmed_event(session, user, **event) -> Event:
    draft = await svc.create(session, user, draft_input_from_parsed(parsed_create(**event)), now=NOW)
    return (await svc.confirm(session, user, draft.id, draft.draft_version, now=NOW)).event


async def count_events(session) -> int:
    return await session.scalar(select(func.count()).select_from(Event))


async def test_a02_create_applies_defaults_and_stores_utc(session, world):
    draft = await svc.create(session, world.me, draft_input_from_parsed(parsed_create()), now=NOW)

    assert draft.status == "awaiting_confirmation"
    assert draft.payload_json["end_local"] == "2026-11-01T15:00:00"
    assert draft.payload_json["defaults_applied"] == ["duration_minutes=60", "visibility=busy_only"]
    assert await count_events(session) == 0  # 未確認前沒有正式行程

    result = await svc.confirm(session, world.me, draft.id, 1, now=NOW, request_id="req-1")
    ev = result.event
    assert ev.start_at == datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
    assert ev.end_at == datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)
    assert (ev.visibility, ev.location, ev.version, ev.source) == ("busy_only", None, 1, "conversation")
    assert draft.status == "committed" and draft.committed_event_id == ev.id

    log = await session.scalar(select(AuditLog))
    assert (log.action, log.changed_fields, log.request_id) == ("event.create", ["created"], "req-1")
    assert "國網" not in str(log.changed_fields)  # 稽核紀錄不存原文（P8）


async def test_missing_start_keeps_collecting_and_cannot_confirm(session, world):
    draft = await svc.create(session, world.me, draft_input_from_parsed(parsed_create(start_local=None)), now=NOW)
    assert draft.status == "collecting"
    assert draft.payload_json["missing_fields"] == ["start_local"]
    with pytest.raises(InvalidDraft):
        await svc.confirm(session, world.me, draft.id, 1, now=NOW)


async def test_end_before_start_blocks_confirmation(session, world):
    data = draft_input_from_parsed(parsed_create(end_local=datetime(2026, 11, 1, 13, 0)))
    draft = await svc.create(session, world.me, data, now=NOW)
    assert draft.payload_json["missing_fields"] == ["end_local"]


async def test_followup_answer_completes_draft_and_bumps_version(session, world):
    draft = await svc.create(session, world.me, draft_input_from_parsed(parsed_create(start_local=None)), now=NOW)
    draft = await svc.update(session, world.me, draft.id, 1,
                             DraftFields(start_local=datetime(2026, 11, 1, 15, 30)), now=NOW)
    assert (draft.status, draft.draft_version) == ("awaiting_confirmation", 2)
    assert draft.payload_json["title"] == "國網中心拜訪"  # 合併到原草稿


async def test_a06_double_confirm_creates_one_event(session, world):
    draft = await svc.create(session, world.me, draft_input_from_parsed(parsed_create()), now=NOW)
    first = await svc.confirm(session, world.me, draft.id, 1, now=NOW)
    again = await svc.confirm(session, world.me, draft.id, 1, now=NOW)

    assert again.already_committed and again.event.id == first.event.id
    assert await count_events(session) == 1


async def test_a07_stale_card_rejected(session, world):
    draft = await svc.create(session, world.me, draft_input_from_parsed(parsed_create()), now=NOW)
    await svc.update(session, world.me, draft.id, 1, DraftFields(title="改過的標題"), now=NOW)
    with pytest.raises(DraftVersionConflict):
        await svc.confirm(session, world.me, draft.id, 1, now=NOW)  # 舊確認卡


async def test_expired_and_abandoned_drafts_are_gone(session, world):
    draft = await svc.create(session, world.me, draft_input_from_parsed(parsed_create()), now=NOW)
    with pytest.raises(DraftGone):
        await svc.confirm(session, world.me, draft.id, 1, now=NOW + timedelta(minutes=31))

    other = await svc.create(session, world.me, draft_input_from_parsed(parsed_create()), now=NOW)
    await svc.abandon(session, world.me, other.id, 1, now=NOW)
    with pytest.raises(DraftGone):
        await svc.confirm(session, world.me, other.id, 1, now=NOW)


async def test_someone_elses_draft_is_not_found(session, world):
    draft = await svc.create(session, world.mate, draft_input_from_parsed(parsed_create()), now=NOW)
    with pytest.raises(NotFound):
        await svc.confirm(session, world.me, draft.id, 1, now=NOW)


async def test_a15_inactive_member_cannot_confirm(session, world):
    draft = await svc.create(session, world.me, draft_input_from_parsed(parsed_create()), now=NOW)
    world.me.status = "suspended"
    with pytest.raises(MemberInactive):
        await svc.confirm(session, world.me, draft.id, 1, now=NOW)


async def test_all_day_stored_as_exclusive_date_range(session, world):
    ev = await confirmed_event(session, world.me, title="員工旅遊",
                               start_local=datetime(2026, 11, 10, 0, 0), all_day=True)
    assert (ev.all_day, ev.start_date, ev.end_date, ev.start_at) == (True, date(2026, 11, 10), date(2026, 11, 11), None)


async def test_update_start_only_keeps_duration(session, world):
    ev = await confirmed_event(session, world.me, end_local=datetime(2026, 11, 1, 16, 0))  # 14:00–16:00
    draft = await svc.create(session, world.me, DraftInput("update", DraftFields(
        start_local=datetime(2026, 11, 1, 15, 0)), event_id=ev.id), now=NOW)
    result = await svc.confirm(session, world.me, draft.id, 1, now=NOW)

    assert result.event.start_at == datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)
    assert result.event.end_at == datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)  # 保留 2 小時
    assert result.event.version == 2
    log = (await session.scalars(select(AuditLog).where(AuditLog.action == "event.update"))).one()
    assert log.changed_fields == ["start_at", "end_at"]


async def test_update_rejected_when_event_changed_since_draft(session, world):
    ev = await confirmed_event(session, world.me)
    stale = await svc.create(session, world.me, DraftInput("update", DraftFields(title="A"), event_id=ev.id), now=NOW)
    fresh = await svc.create(session, world.me, DraftInput("update", DraftFields(title="B"), event_id=ev.id), now=NOW)
    await svc.confirm(session, world.me, fresh.id, 1, now=NOW)
    with pytest.raises(EventVersionConflict):
        await svc.confirm(session, world.me, stale.id, 1, now=NOW)


async def test_cancel_marks_event_cancelled(session, world):
    ev = await confirmed_event(session, world.me)
    draft = await svc.create(session, world.me, DraftInput("cancel", event_id=ev.id), now=NOW)
    result = await svc.confirm(session, world.me, draft.id, 1, now=NOW)
    assert (result.event.status, result.event.version) == ("cancelled", 2)
    with pytest.raises(InvalidDraft):
        await svc.create(session, world.me, DraftInput("cancel", event_id=ev.id), now=NOW)


async def test_cannot_draft_changes_to_someone_elses_event(session, world):
    theirs = await confirmed_event(session, world.mate)
    with pytest.raises(NotFound):
        await svc.create(session, world.me, DraftInput("cancel", event_id=theirs.id), now=NOW)


async def test_query_intent_does_not_make_a_draft():
    with pytest.raises(InvalidDraft):
        draft_input_from_parsed(ParsedSchedule(intent="query"))
