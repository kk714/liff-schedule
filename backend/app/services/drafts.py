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
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Event, EventDraft, User
from app.llm.schemas import ParsedSchedule

from .errors import DraftGone, DraftVersionConflict, EventVersionConflict, InvalidDraft, MemberInactive, NotFound

DEFAULT_DURATION = timedelta(minutes=60)
DRAFT_TTL = timedelta(minutes=30)
EVENT_FIELDS = ("title", "start_local", "end_local", "all_day", "location", "notes", "visibility")

Visibility = Literal["private", "busy_only", "team_detail"]


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


@dataclass
class ConfirmResult:
    event: Event
    intent: str
    already_committed: bool = False


@dataclass
class DraftInput:
    intent: Literal["create", "update", "cancel"]
    fields: DraftFields = field(default_factory=DraftFields)
    event_id: uuid.UUID | None = None


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
        data["end_local"] = None
    elif data["start_local"] and data["end_local"] is None:
        data["end_local"] = data["start_local"] + DEFAULT_DURATION
        defaults.append("duration_minutes=60")
    if data["visibility"] is None:
        data["visibility"] = user.default_visibility
        defaults.append(f"visibility={user.default_visibility}")
    if data["start_local"] and data["end_local"] and data["end_local"] <= data["start_local"]:
        missing.append("end_local")  # 結束早於開始：阻擋並追問（§6）
    return data, defaults, missing


def _serialize(data: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in data.items()}


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
        else:
            event = await self._own_event(session, user, data.event_id, lock=False)
            if event.status != "active":
                raise InvalidDraft("target event is cancelled")
            base_version = event.version
            payload = data.fields.model_dump(exclude_none=True)
            defaults, missing = [], []
            if data.intent == "update" and not payload:
                missing = ["changes"]

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
        fields: DraftFields, *, now: datetime,
    ) -> EventDraft:
        """表單或追問修改草稿；內容變更遞增版本，舊確認卡隨之失效（§5.3）。"""
        _ensure_active(user)
        draft = await self._lock_draft(session, user, draft_id)
        self._check_open(draft, draft_version, now)

        if draft.intent == "create":
            merged = _load_fields(draft).model_copy(update=fields.model_dump(exclude_none=True))
            payload, defaults, missing = _resolve_create(merged, user)
        else:
            payload = {**{k: v for k, v in draft.payload_json.items() if k in EVENT_FIELDS},
                       **_serialize(fields.model_dump(exclude_none=True))}
            defaults, missing = [], ([] if payload or draft.intent == "cancel" else ["changes"])
        draft.payload_json = {**_serialize(payload), "defaults_applied": defaults, "missing_fields": missing}
        draft.status = "collecting" if missing else "awaiting_confirmation"
        draft.draft_version += 1
        draft.expires_at = now + self._ttl
        await session.flush()
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
        if draft.intent == "create":
            event = Event(organization_id=user.organization_id, owner_user_id=user.id,
                          source="conversation", timezone=user.timezone,
                          title=fields.title, visibility=fields.visibility)
            _apply_time(event, fields.start_local, fields.end_local, bool(fields.all_day), tz)
            event.location, event.notes = fields.location, fields.notes
            session.add(event)
            changed = ["created"]
        else:
            event = await self._own_event(session, user, draft.event_id, lock=True)
            if event.status != "active":
                raise EventVersionConflict("target event is already cancelled")
            if event.version != draft.base_event_version:
                raise EventVersionConflict("target event changed after the draft was created")
            changed = _apply_update(event, fields, tz) if draft.intent == "update" else ["status"]
            if draft.intent == "cancel":
                event.status = "cancelled"
            event.version += 1

        await session.flush()
        draft.status = "committed"
        draft.committed_event_id = event.id
        session.add(AuditLog(
            organization_id=user.organization_id, actor_user_id=user.id,
            action=f"event.{draft.intent}", target_type="event", target_id=event.id,
            changed_fields=changed, request_id=request_id, created_at=now,
        ))
        await session.flush()
        return ConfirmResult(event=event, intent=draft.intent)

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
