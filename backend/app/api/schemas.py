"""API 輸出格式。時間一律以「使用者時區的當地時間」字串（YYYY-MM-DDTHH:MM:SS）呈現，
同時提供帶 offset 的 ISO 8601（§6）；全天行程的 end_date 不含當日。"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from app.db.models import Event, EventDraft
from app.services import EventView

DRAFT_FIELDS = ("title", "start_local", "end_local", "all_day", "location", "notes", "visibility")


def _local(value, tz: ZoneInfo) -> str | None:
    return value.astimezone(tz).replace(tzinfo=None).isoformat(timespec="seconds") if value else None


def event_out(ev: Event, tz: ZoneInfo) -> dict[str, Any]:
    return {
        "id": str(ev.id), "title": ev.title, "all_day": ev.all_day,
        "start_local": _local(ev.start_at, tz), "end_local": _local(ev.end_at, tz),
        "start_date": ev.start_date.isoformat() if ev.start_date else None,
        "end_date": ev.end_date.isoformat() if ev.end_date else None,
        "location": ev.location, "notes": ev.notes, "visibility": ev.visibility,
        "status": ev.status, "version": ev.version,
    }


def draft_out(draft: EventDraft, event: Event | None, tz: ZoneInfo) -> dict[str, Any]:
    p = draft.payload_json
    return {
        "id": str(draft.id), "intent": draft.intent, "status": draft.status,
        "draft_version": draft.draft_version, "expires_at": draft.expires_at.isoformat(),
        "fields": {k: p.get(k) for k in DRAFT_FIELDS},
        "defaults_applied": p.get("defaults_applied", []),
        "missing_fields": p.get("missing_fields", []),
        "event": event_out(event, tz) if event is not None else None,
    }


def view_out(v: EventView, viewer_id, tz: ZoneInfo) -> dict[str, Any]:
    return {
        "id": str(v.id), "owner_user_id": str(v.owner_user_id), "owner_name": v.owner_name,
        "mine": v.owner_user_id == viewer_id, "projection": v.projection, "all_day": v.all_day,
        "start": v.start_at.isoformat() if v.start_at else None,
        "end": v.end_at.isoformat() if v.end_at else None,
        "start_local": _local(v.start_at, tz), "end_local": _local(v.end_at, tz),
        "start_date": v.start_date.isoformat() if v.start_date else None,
        "end_date": v.end_date.isoformat() if v.end_date else None,
        "title": v.title, "location": v.location, "notes": v.notes, "visibility": v.visibility,
    }
