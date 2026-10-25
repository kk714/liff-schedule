"""行程查詢與分享權限（設計文件 §7）。權限條件直接寫進 SQL，不先撈全部再遮蔽（§7.2-3）。

    可見 = 同組織 AND status=active AND (
             本人
          OR (visibility ∈ {busy_only, team_detail}
              AND 擁有者為啟用成員（停權者隱藏，P6）
              AND 查詢者與擁有者「目前」至少共屬一個啟用團隊（具追溯性，P6）)
          )
    投影：本人或 team_detail → 詳細；busy_only → 只有人員與時段
    關鍵字：只比對本人與 team_detail 的標題（不讓私人／忙碌行程的標題命中洩漏，§7.2-5、A08）
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import Event, Team, TeamMember, User

from .errors import MemberInactive, NotFound, RangeTooLarge

MAX_RANGE = timedelta(days=90)
PAGE_SIZE = 100


@dataclass(frozen=True)
class EventView:
    id: uuid.UUID
    owner_user_id: uuid.UUID
    owner_name: str
    projection: Literal["detail", "busy"]
    all_day: bool
    start_at: datetime | None
    end_at: datetime | None
    start_date: date | None
    end_date: date | None
    title: str | None = None
    location: str | None = None
    notes: str | None = None
    visibility: str | None = None  # 只有本人看得到自己的分享設定


@dataclass(frozen=True)
class EventPage:
    items: list[EventView]
    has_more: bool


def _shares_team(viewer_id: uuid.UUID):
    mine, theirs = aliased(TeamMember), aliased(TeamMember)
    return exists(
        select(1)
        .select_from(mine)
        .join(theirs, theirs.team_id == mine.team_id)
        .join(Team, Team.id == mine.team_id)
        .where(mine.user_id == viewer_id, theirs.user_id == Event.owner_user_id, Team.status == "active")
    )


def _visible(viewer: User):
    return and_(
        Event.organization_id == viewer.organization_id,
        Event.status == "active",
        or_(
            Event.owner_user_id == viewer.id,
            and_(
                Event.visibility.in_(("busy_only", "team_detail")),
                User.status == "active",
                _shares_team(viewer.id),
            ),
        ),
    )


def _overlaps(start: datetime, end: datetime, tz: ZoneInfo):
    """區間交集：非全天用時間瞬間，全天用查詢者時區的日期（§6）。"""
    first_day = start.astimezone(tz).date()
    local_end = end.astimezone(tz)
    end_day = local_end.date() + (timedelta(days=1) if local_end.time() != time.min else timedelta(0))
    return or_(
        and_(Event.all_day.is_(False), Event.start_at < end, Event.end_at > start),
        and_(Event.all_day.is_(True), Event.start_date < end_day, Event.end_date > first_day),
    )


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _view(event: Event, owner_name: str, viewer: User) -> EventView:
    own = event.owner_user_id == viewer.id
    detail = own or event.visibility == "team_detail"
    return EventView(
        id=event.id, owner_user_id=event.owner_user_id, owner_name=owner_name,
        projection="detail" if detail else "busy", all_day=event.all_day,
        start_at=event.start_at, end_at=event.end_at, start_date=event.start_date, end_date=event.end_date,
        title=event.title if detail else None,
        location=event.location if detail else None,
        notes=event.notes if detail else None,
        visibility=event.visibility if own else None,
    )


class EventQueryService:
    async def list_visible(
        self, session: AsyncSession, viewer: User, *, start: datetime, end: datetime,
        member_ids: list[uuid.UUID] | None = None, team_id: uuid.UUID | None = None,
        keyword: str | None = None, limit: int = PAGE_SIZE, offset: int = 0,
    ) -> EventPage:
        if viewer.status != "active":
            raise MemberInactive("member is not active")
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start/end must be timezone-aware")
        if end <= start or end - start > MAX_RANGE:
            raise RangeTooLarge("range must be positive and at most 90 days")
        limit = max(1, min(limit, PAGE_SIZE))

        stmt = (
            select(Event, User.display_name)
            .join(User, User.id == Event.owner_user_id)
            .where(_visible(viewer), _overlaps(start, end, ZoneInfo(viewer.timezone)))
        )
        if member_ids:
            stmt = stmt.where(Event.owner_user_id.in_(member_ids))
        if team_id is not None:
            stmt = stmt.where(
                exists(select(1).where(TeamMember.team_id == team_id,
                                       TeamMember.user_id == Event.owner_user_id))
            )
        if keyword:
            stmt = stmt.where(
                or_(Event.owner_user_id == viewer.id, Event.visibility == "team_detail"),
                Event.title.ilike(f"%{_escape_like(keyword)}%", escape="\\"),
            )
        # 全天行程以日期 00:00（UTC）近似排序；同一天內排在非全天之前
        stmt = stmt.order_by(Event.start_date.is_(None), Event.start_date, Event.start_at, Event.id)
        rows = (await session.execute(stmt.offset(offset).limit(limit + 1))).all()
        items = [_view(ev, name, viewer) for ev, name in rows[:limit]]
        return EventPage(items=items, has_more=len(rows) > limit)

    async def get_visible(self, session: AsyncSession, viewer: User, event_id: uuid.UUID) -> EventView:
        if viewer.status != "active":
            raise MemberInactive("member is not active")
        row = (
            await session.execute(
                select(Event, User.display_name)
                .join(User, User.id == Event.owner_user_id)
                .where(Event.id == event_id, _visible(viewer))
            )
        ).first()
        if row is None:
            raise NotFound("event not found")  # 看不到與不存在一律 404
        return _view(row[0], row[1], viewer)
