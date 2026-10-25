"""行程查詢、可查成員與前端設定 API（設計文件 §7、§9）。權限一律由 EventQueryService 在 SQL 套用。"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.db.models import Team, TeamMember, User
from app.services import EventQueryService

from .auth import current_user
from .schemas import view_out

router = APIRouter(prefix="/api")
queries = EventQueryService()


@router.get("/config")
async def config(request: Request) -> dict:
    """前端初始化 LIFF 用；不含任何機密。"""
    return {"liff_id": request.app.state.settings.liff_id}


@router.get("/events")
async def list_events(
    request: Request,
    start_date: date,
    end_date: date = Query(description="含當日"),
    member_id: list[uuid.UUID] = Query(default_factory=list),
    team_id: uuid.UUID | None = None,
    keyword: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
) -> dict:
    tz = ZoneInfo(user.timezone)
    start = datetime.combine(start_date, datetime.min.time(), tz)
    end = datetime.combine(max(end_date, start_date) + timedelta(days=1), datetime.min.time(), tz)
    async with request.app.state.sessions() as s:
        page = await queries.list_visible(s, user, start=start, end=end, member_ids=member_id or None,
                                          team_id=team_id, keyword=keyword or None, limit=limit, offset=offset)
        await s.rollback()
    return {"items": [view_out(v, user.id, tz) for v in page.items], "has_more": page.has_more,
            "next_offset": offset + len(page.items) if page.has_more else None}


@router.get("/events/{event_id}")
async def get_event(event_id: uuid.UUID, request: Request, user: User = Depends(current_user)) -> dict:
    async with request.app.state.sessions() as s:
        view = await queries.get_visible(s, user, event_id)
        await s.rollback()
    return view_out(view, user.id, ZoneInfo(user.timezone))


@router.get("/members")
async def members(request: Request, team_id: uuid.UUID | None = None, user: User = Depends(current_user)) -> dict:
    """可查成員：本人與至少共屬一個啟用團隊的啟用成員（§7.1）。"""
    mine, theirs = aliased(TeamMember), aliased(TeamMember)
    stmt = (
        select(User.id, User.display_name, Team.id, Team.name)
        .join(theirs, theirs.user_id == User.id)
        .join(Team, Team.id == theirs.team_id)
        .join(mine, (mine.team_id == theirs.team_id) & (mine.user_id == user.id))
        .where(User.status == "active", Team.status == "active", User.organization_id == user.organization_id)
        .order_by(Team.name, User.display_name)
    )
    if team_id is not None:
        stmt = stmt.where(Team.id == team_id)
    async with request.app.state.sessions() as s:
        rows = (await s.execute(stmt)).all()
        await s.rollback()
    people: dict[uuid.UUID, dict] = {}
    for uid, name, tid, tname in rows:
        entry = people.setdefault(uid, {"id": str(uid), "display_name": name, "me": uid == user.id, "teams": []})
        entry["teams"].append({"id": str(tid), "name": tname})
    if user.id not in people and team_id is None:
        people[user.id] = {"id": str(user.id), "display_name": user.display_name, "me": True, "teams": []}
    return {"members": sorted(people.values(), key=lambda m: (not m["me"], m["display_name"]))}
