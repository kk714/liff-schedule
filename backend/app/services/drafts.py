"""草稿服務：建立、表單修改、確認交易、放棄（設計文件 §5、§8.1、§9.2）。

確認交易（呼叫端負責 commit／rollback）：

    鎖草稿列（FOR UPDATE；SQLite 測試時無效，並行正確性於 PostgreSQL 驗收）
      ├─ 已 committed 且版本相同 → 回傳既有結果（連點／網路重試，A06）
      ├─ 版本不符 → DraftVersionConflict(409)
      ├─ 已放棄／已過期 → DraftGone(410)
      ├─ 不是 awaiting_confirmation → InvalidDraft(422)
      ├─ create → 新增 events
      └─ update／cancel → 鎖目標行程，檢查 owner、狀態與 base_event_version → 套用並 version+1
    草稿標記 committed、寫 audit_logs（只記欄位名稱，P8）

所有時間在服務層以「使用者時區的當地時間」進出，存檔前轉 UTC。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import AuditLog, Event, EventAttendee, EventDraft, Team, TeamMember, User
from app.llm.schemas import ParsedSchedule

from .errors import DraftGone, DraftVersionConflict, EventVersionConflict, InvalidDraft, MemberInactive, NotFound

DEFAULT_DURATION = timedelta(minutes=60)
DRAFT_TTL = timedelta(minutes=30)
EVENT_FIELDS = ("title", "start_local", "end_local", "all_day", "location", "notes", "visibility",
                "recurrence", "attendee_ids", "scope")
MAX_RECURRENCE_DAYS = 183  # 每週重複最多展開約 6 個月
MAX_OCCURRENCES = 60
MAX_ATTENDEES = 50

Visibility = Literal["private", "busy_only", "team_detail"]


class Recurrence(BaseModel):
    """每週重複（只在 LIFF 手動建立時提供）。weekdays：0=週一 … 6=週日；until 含當日。"""

    model_config = ConfigDict(extra="forbid")

    freq: Literal["weekly"] = "weekly"
    weekdays: list[int] = Field(min_length=1, max_length=7)
    until: date


class DraftFields(BaseModel):
    """草稿內容。create 草稿為完整欄位；update 草稿只含要改的欄位（未提供即 None）。"""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=100)
    start_local: datetime | None = None
    end_local: datetime | None = None
    all_day: bool | None = None
    location: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=1000)
    visibility: Visibility | None = None
    recurrence: Recurrence | None = None
    attendee_ids: list[uuid.UUID] | None = Field(default=None, max_length=MAX_ATTENDEES)
    scope: Literal["single", "following"] | None = None  # 取消系列行程時：只這次／這次與之後


@dataclass
class ConfirmResult:
    event: Event
    intent: str
    already_committed: bool = False
    affected: int = 1  # 這次建立或取消的行程筆數（每週重複、取消之後全部時大於 1）


@dataclass
class DraftInput:
    intent: Literal["create", "update", "cancel"]
    fields: DraftFields = field(default_factory=DraftFields)
    event_id: uuid.UUID | None = None
    # 欄位完整但仍需追問的項目（例如年份不明、「這週X」已過），草稿維持 collecting
    blockers: list[str] = field(default_factory=list)


def draft_input_from_parsed(parsed: ParsedSchedule) -> DraftInput:
    """把模型解析結果轉成草稿輸入；update／cancel 的目標行程由呼叫端解析後再指定 event_id。"""
    if parsed.intent not in ("create", "update", "cancel"):
        raise InvalidDraft(f"intent {parsed.intent} does not produce a draft")
    ev = parsed.event
    fields = DraftFields(
        title=ev.title if ev else None,
        start_local=ev.start_local if ev else None,
        end_local=ev.end_local if ev else None,
        all_day=ev.all_day if ev and parsed.intent == "create" else None,
        location=ev.location if ev else None,
        notes=ev.notes if ev else None,
        visibility=ev.visibility if ev else None,
    )
    return DraftInput(intent=parsed.intent, fields=fields)


def _ensure_active(user: User) -> None:
    if user.status != "active":
        raise MemberInactive("member is not active")


def _resolve_create(fields: DraftFields, user: User) -> tuple[dict[str, Any], list[str], list[str]]:
    """套用預設值；回傳 (payload, defaults_applied, missing)。"""
    data = fields.model_dump()
    defaults: list[str] = []
    missing = [name for name in ("title", "start_local") if not data[name]]
    data["all_day"] = bool(data["all_day"])
    if data["start_local"] and data["all_day"]:
        data["start_local"] = datetime.combine(data["start_local"].date(), datetime.min.time())
        # 多天行程（例如出差）：end_local 為含當日的最後一天
        end = data["end_local"]
        data["end_local"] = (datetime.combine(end.date(), datetime.min.time())
                             if end is not None and end.date() > data["start_local"].date() else None)
    elif data["start_local"] and data["end_local"] is None:
        data["end_local"] = data["start_local"] + DEFAULT_DURATION
        defaults.append("duration_minutes=60")
    if data["visibility"] is None:
        # 分享範圍不需使用者選擇，直接套用成員預設（預設 team_detail），不列入提示
        data["visibility"] = user.default_visibility
    if (not data["all_day"] and data["start_local"] and data["end_local"]
            and data["end_local"] <= data["start_local"]):
        missing.append("end_local")  # 結束早於開始：阻擋並追問（§6）
    if data["recurrence"] is not None and data["start_local"] is not None:
        try:
            occurrences(data["start_local"].date(), Recurrence.model_validate(data["recurrence"]))
        except InvalidDraft:
            missing.append("recurrence")
    if data["attendee_ids"] is not None:
        data["attendee_ids"] = list(dict.fromkeys(a for a in data["attendee_ids"] if a != user.id))
    data["scope"] = None
    return data, defaults, missing


def occurrences(first: date, rule: Recurrence) -> list[date]:
    """每週重複展開成日期清單；第一筆為起始日當天或之後第一個符合的星期。"""
    if sorted(set(rule.weekdays)) != sorted(rule.weekdays) or not all(0 <= d <= 6 for d in rule.weekdays):
        raise InvalidDraft("weekdays must be unique values 0-6")
    if rule.until < first or (rule.until - first).days > MAX_RECURRENCE_DAYS:
        raise InvalidDraft("recurrence must end within about 6 months")
    days = [first + timedelta(days=i) for i in range((rule.until - first).days + 1)]
    result = [d for d in days if d.weekday() in rule.weekdays]
    if not result:
        raise InvalidDraft("no occurrence in range")
    if len(result) > MAX_OCCURRENCES:
        raise InvalidDraft(f"at most {MAX_OCCURRENCES} occurrences")
    return result


def _serialize(value: Any) -> Any:
    """草稿 payload 存成 JSON：日期時間轉 ISO 字串、UUID 轉字串，遞迴處理巢狀結構。"""
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, BaseModel):
        return _serialize(value.model_dump())
    return value


def _load_fields(draft: EventDraft) -> DraftFields:
    try:
        return DraftFields.model_validate({k: draft.payload_json.get(k) for k in EVENT_FIELDS})
    except ValidationError as exc:
        raise InvalidDraft(str(exc)) from exc


class DraftService:
    def __init__(self, ttl: timedelta = DRAFT_TTL) -> None:
        self._ttl = ttl

    async def create(self, session: AsyncSession, user: User, data: DraftInput, *, now: datetime) -> EventDraft:
        _ensure_active(user)
        base_version = None
        if data.intent == "create":
            payload, defaults, missing = _resolve_create(data.fields, user)
            missing += await self._attendee_problems(session, user, payload.get("attendee_ids"))
        else:
            event = await self._own_event(session, user, data.event_id, lock=False)
            if event.status != "active":
                raise InvalidDraft("target event is cancelled")
            base_version = event.version
            payload = data.fields.model_dump(exclude_none=True)
            defaults, missing = [], []
            if data.intent == "update" and not payload:
                missing = ["changes"]
        missing = missing + [b for b in data.blockers if b not in missing]

        draft = EventDraft(
            organization_id=user.organization_id,
            user_id=user.id,
            intent=data.intent,
            event_id=data.event_id,
            base_event_version=base_version,
            payload_json={**_serialize(payload), "defaults_applied": defaults, "missing_fields": missing},
            status="collecting" if missing else "awaiting_confirmation",
            expires_at=now + self._ttl,
        )
        session.add(draft)
        await session.flush()
        return draft

    async def update(
        self, session: AsyncSession, user: User, draft_id: uuid.UUID, draft_version: int,
        fields: DraftFields, *, now: datetime, blockers: list[str] | None = None,
        clear: set[str] | None = None,
    ) -> EventDraft:
        """表單或追問修改草稿；內容變更遞增版本，舊確認卡隨之失效（§5.3）。"""
        _ensure_active(user)
        draft = await self._lock_draft(session, user, draft_id)
        self._check_open(draft, draft_version, now)

        if draft.intent == "create":
            # 一般合併只採用有值的欄位；clear 列出的欄位（表單明確清空，例如取消重複）設回 None
            updates = {**fields.model_dump(exclude_none=True), **{k: None for k in (clear or set())}}
            merged = _load_fields(draft).model_copy(update=updates)
            payload, defaults, missing = _resolve_create(merged, user)
            missing += await self._attendee_problems(session, user, payload.get("attendee_ids"))
        else:
            payload = {**{k: v for k, v in draft.payload_json.items() if k in EVENT_FIELDS},
                       **_serialize(fields.model_dump(exclude_none=True))}
            defaults, missing = [], ([] if payload or draft.intent == "cancel" else ["changes"])
        missing = missing + [b for b in (blockers or []) if b not in missing]
        draft.payload_json = {**_serialize(payload), "defaults_applied": defaults, "missing_fields": missing}
        draft.status = "collecting" if missing else "awaiting_confirmation"
        draft.draft_version += 1
        draft.expires_at = now + self._ttl
        await session.flush()
        return draft

    async def get(self, session: AsyncSession, user: User, draft_id: uuid.UUID) -> EventDraft:
        draft = await session.scalar(select(EventDraft).where(
            EventDraft.id == draft_id, EventDraft.user_id == user.id,
            EventDraft.organization_id == user.organization_id))
        if draft is None:
            raise NotFound("draft not found")
        return draft

    async def abandon(
        self, session: AsyncSession, user: User, draft_id: uuid.UUID, draft_version: int, *, now: datetime
    ) -> None:
        draft = await self._lock_draft(session, user, draft_id)
        self._check_open(draft, draft_version, now)
        draft.status = "abandoned"
        await session.flush()

    async def confirm(
        self, session: AsyncSession, user: User, draft_id: uuid.UUID, draft_version: int,
        *, now: datetime, request_id: str | None = None,
    ) -> ConfirmResult:
        _ensure_active(user)
        draft = await self._lock_draft(session, user, draft_id)

        if draft.status == "committed":
            if draft.draft_version != draft_version:
                raise DraftVersionConflict("draft was changed before it was committed")
            event = await session.get(Event, draft.committed_event_id)
            return ConfirmResult(event=event, intent=draft.intent, already_committed=True)
        self._check_open(draft, draft_version, now)
        if draft.status != "awaiting_confirmation":
            raise InvalidDraft(f"draft is missing {draft.payload_json.get('missing_fields')}")

        fields = _load_fields(draft)
        tz = ZoneInfo(user.timezone)
        attendees = await self._valid_attendees(session, user, fields.attendee_ids)
        affected = 1
        if draft.intent == "create":
            dates = occurrences(fields.start_local.date(), fields.recurrence) if fields.recurrence else None
            series_id = uuid.uuid4() if dates else None
            events = []
            for day in dates or [fields.start_local.date()]:
                shift = timedelta(days=(day - fields.start_local.date()).days)
                ev = Event(organization_id=user.organization_id, owner_user_id=user.id,
                           source="conversation", timezone=user.timezone, series_id=series_id,
                           title=fields.title, visibility=fields.visibility,
                           location=fields.location, notes=fields.notes)
                _apply_time(ev, fields.start_local + shift, fields.end_local + shift if fields.end_local else None,
                            bool(fields.all_day), tz)
                session.add(ev)
                events.append(ev)
            await session.flush()
            for ev in events:
                self._add_attendees(session, user, ev, attendees)
            event, affected = events[0], len(events)
            changed = ["created"] + (["recurrence"] if dates else []) + (["attendees"] if attendees else [])
        else:
            event = await self._own_event(session, user, draft.event_id, lock=True)
            if event.status != "active":
                raise EventVersionConflict("target event is already cancelled")
            if event.version != draft.base_event_version:
                raise EventVersionConflict("target event changed after the draft was created")
            if draft.intent == "update":
                changed = _apply_update(event, fields, tz)
                if fields.attendee_ids is not None:
                    await self._sync_attendees(session, user, event, attendees)
                    changed.append("attendees")
                event.version += 1
            else:
                changed = ["status"]
                targets = [event]
                if fields.scope == "following" and event.series_id is not None:
                    targets = await self._following(session, user, event)
                for ev in targets:
                    ev.status = "cancelled"
                    ev.version += 1
                affected = len(targets)

        await session.flush()
        draft.status = "committed"
        draft.committed_event_id = event.id
        session.add(AuditLog(
            organization_id=user.organization_id, actor_user_id=user.id,
            action=f"event.{draft.intent}", target_type="event", target_id=event.id,
            changed_fields=changed, request_id=request_id, created_at=now,
        ))
        await session.flush()
        return ConfirmResult(event=event, intent=draft.intent, affected=affected)

    async def _attendee_problems(self, session: AsyncSession, user: User, ids) -> list[str]:
        try:
            await self._valid_attendees(session, user, ids)
        except InvalidDraft:
            return ["attendees"]
        return []

    async def _valid_attendees(self, session: AsyncSession, user: User, ids) -> list[uuid.UUID]:
        """受邀者必須是同組織、啟用中，且與主辦人至少共屬一個啟用團隊的成員。"""
        if not ids:
            return []
        ids = [i for i in dict.fromkeys(ids) if i != user.id]
        mine, theirs = aliased(TeamMember), aliased(TeamMember)
        ok = set((await session.scalars(
            select(User.id)
            .join(theirs, theirs.user_id == User.id)
            .join(mine, (mine.team_id == theirs.team_id) & (mine.user_id == user.id))
            .join(Team, Team.id == theirs.team_id)
            .where(User.id.in_(ids), User.organization_id == user.organization_id,
                   User.status == "active", Team.status == "active")
        )).all())
        if set(ids) - ok:
            raise InvalidDraft("attendees must be active teammates")
        return ids

    def _add_attendees(self, session: AsyncSession, user: User, event: Event, ids: list[uuid.UUID]) -> None:
        for uid in ids:
            session.add(EventAttendee(organization_id=user.organization_id, event_id=event.id,
                                      user_id=uid, added_by_user_id=user.id))

    async def _sync_attendees(self, session: AsyncSession, user: User, event: Event, ids: list[uuid.UUID]) -> None:
        current = {a.user_id: a for a in (await session.scalars(
            select(EventAttendee).where(EventAttendee.event_id == event.id))).all()}
        for uid, row in current.items():
            if uid not in ids:
                await session.delete(row)
        self._add_attendees(session, user, event, [i for i in ids if i not in current])

    async def _following(self, session: AsyncSession, user: User, event: Event) -> list[Event]:
        """同系列中這一筆與之後、尚未取消的本人行程。"""
        later = (Event.start_date >= event.start_date) if event.all_day else (Event.start_at >= event.start_at)
        return list((await session.scalars(
            select(Event).where(Event.series_id == event.series_id, Event.owner_user_id == user.id,
                                Event.status == "active", later).with_for_update()
        )).all())

    async def _lock_draft(self, session: AsyncSession, user: User, draft_id: uuid.UUID) -> EventDraft:
        draft = await session.scalar(
            select(EventDraft)
            .where(EventDraft.id == draft_id, EventDraft.user_id == user.id,
                   EventDraft.organization_id == user.organization_id)
            .with_for_update()
        )
        if draft is None:
            raise NotFound("draft not found")
        return draft

    async def _own_event(self, session: AsyncSession, user: User, event_id, *, lock: bool) -> Event:
        if event_id is None:
            raise InvalidDraft("update/cancel draft requires event_id")
        stmt = select(Event).where(Event.id == event_id, Event.owner_user_id == user.id,
                                   Event.organization_id == user.organization_id)
        event = await session.scalar(stmt.with_for_update() if lock else stmt)
        if event is None:
            raise NotFound("event not found")  # 他人的行程同樣回 404，不揭露存在與否
        return event

    @staticmethod
    def _check_open(draft: EventDraft, draft_version: int, now: datetime) -> None:
        if draft.status in ("abandoned", "expired") or draft.expires_at <= now:
            raise DraftGone("draft expired or abandoned")
        if draft.status == "committed":
            raise DraftVersionConflict("draft already committed")
        if draft.draft_version != draft_version:
            raise DraftVersionConflict("stale draft_version")


def _apply_time(event: Event, start: datetime | None, end: datetime | None, all_day: bool, tz: ZoneInfo) -> None:
    if start is None:
        raise InvalidDraft("start_local is required")
    event.all_day = all_day
    if all_day:
        # end_local 若給，代表含當日的最後一天；存成不含當日的 end_date（§6）
        last_day = end.date() if end is not None and end.date() > start.date() else start.date()
        event.start_date = start.date()
        event.end_date = last_day + timedelta(days=1)
        event.start_at = event.end_at = None
    else:
        if end is None or end <= start:
            raise InvalidDraft("end_local must be after start_local")
        event.start_at = start.replace(tzinfo=tz)
        event.end_at = end.replace(tzinfo=tz)
        event.start_date = event.end_date = None


def _apply_update(event: Event, fields: DraftFields, tz: ZoneInfo) -> list[str]:
    changed: list[str] = []
    for name in ("title", "location", "notes", "visibility"):
        value = getattr(fields, name)
        if value is not None and value != getattr(event, name):
            setattr(event, name, value)
            changed.append(name)

    if fields.start_local is None and fields.end_local is None:
        return changed
    if event.all_day:
        if fields.end_local is not None and fields.start_local is None:
            raise InvalidDraft("all-day event needs a new start date")
        # 全天行程只改日期，保留天數（§5.2）
        span = event.end_date - event.start_date
        event.start_date = fields.start_local.date()
        event.end_date = event.start_date + span
        changed += ["start_date", "end_date"]
        return changed

    old_start = event.start_at.astimezone(tz).replace(tzinfo=None)
    old_end = event.end_at.astimezone(tz).replace(tzinfo=None)
    start = fields.start_local or old_start
    end = fields.end_local or (start + (old_end - old_start))  # 只改開始時間時保留原時長（§5.2）
    _apply_time(event, start, end, False, tz)
    changed += [n for n, old, new in (("start_at", old_start, start), ("end_at", old_end, end)) if old != new]
    return changed
