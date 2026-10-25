"""查詢權限（§7、A08、A09、A13、A19）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.db.models import Event, TeamMember
from app.services import EventQueryService
from app.services.errors import MemberInactive, NotFound, RangeTooLarge

from .conftest import NOW

q = EventQueryService()
UTC = timezone.utc
WEEK = (datetime(2026, 10, 18, 16, tzinfo=UTC), datetime(2026, 10, 25, 16, tzinfo=UTC))  # 台北 10/19–10/26


async def add(session, owner, title, visibility="busy_only", *, start=None, hours=1, all_day=None, status="active"):
    if all_day:
        ev = Event(organization_id=owner.organization_id, owner_user_id=owner.id, title=title, timezone="Asia/Taipei",
                   visibility=visibility, source="manual", all_day=True, start_date=all_day[0], end_date=all_day[1],
                   status=status)
    else:
        start = start or datetime(2026, 10, 22, 2, tzinfo=UTC)
        ev = Event(organization_id=owner.organization_id, owner_user_id=owner.id, title=title, timezone="Asia/Taipei",
                   visibility=visibility, source="manual", start_at=start, end_at=start + timedelta(hours=hours),
                   status=status)
    session.add(ev)
    await session.flush()
    return ev


async def titles(session, viewer, **kw):
    page = await q.list_visible(session, viewer, start=kw.pop("start", WEEK[0]), end=kw.pop("end", WEEK[1]), **kw)
    return {(v.owner_name, v.title, v.projection) for v in page.items}


async def test_a08_visibility_matrix(session, world):
    await add(session, world.me, "我的私事", "private")
    await add(session, world.mate, "同事私事", "private")
    await add(session, world.mate, "同事拜訪客戶", "busy_only")
    await add(session, world.mate, "團隊週會", "team_detail")
    await add(session, world.stranger, "陌生人忙碌", "busy_only")
    await add(session, world.outsider, "外人詳細", "team_detail")

    assert await titles(session, world.me) == {
        ("我", "我的私事", "detail"),          # 本人全部可見
        ("王小明", None, "busy"),             # 僅忙碌：沒有標題
        ("王小明", "團隊週會", "detail"),
    }


async def test_busy_projection_hides_location_and_notes(session, world):
    ev = await add(session, world.mate, "拜訪客戶", "busy_only")
    ev.location, ev.notes = "台北101", "帶合約"
    await session.flush()
    view = await q.get_visible(session, world.me, ev.id)
    assert (view.title, view.location, view.notes, view.visibility) == (None, None, None, None)


async def test_a08_keyword_never_matches_hidden_titles(session, world):
    await add(session, world.mate, "國網中心拜訪", "busy_only")
    await add(session, world.mate, "國網中心簡報", "private")
    await add(session, world.mate, "國網中心週會", "team_detail")
    await add(session, world.me, "國網中心自己去", "private")

    assert await titles(session, world.me, keyword="國網") == {
        ("王小明", "國網中心週會", "detail"),
        ("我", "國網中心自己去", "detail"),
    }


async def test_keyword_wildcards_are_literal(session, world):
    await add(session, world.me, "100% 完成")
    await add(session, world.me, "1000 份報告")
    assert await titles(session, world.me, keyword="100%") == {("我", "100% 完成", "detail")}


async def test_cancelled_events_excluded(session, world):
    await add(session, world.me, "取消的會", status="cancelled")
    assert await titles(session, world.me) == set()


async def test_suspended_owner_hidden_from_team(session, world):
    await add(session, world.mate, "團隊週會", "team_detail")
    world.mate.status = "left"
    await session.flush()
    assert await titles(session, world.me) == set()


async def test_a19_joining_team_reveals_existing_shared_events(session, world):
    await add(session, world.stranger, "研發評審", "team_detail")
    assert await titles(session, world.me) == set()

    session.add(TeamMember(organization_id=world.org.id, team_id=world.other_team.id, user_id=world.me.id))
    await session.flush()
    assert await titles(session, world.me) == {("陌生同事", "研發評審", "detail")}


async def test_a09_other_org_event_not_found_by_id(session, world):
    ev = await add(session, world.outsider, "外人行程", "team_detail")
    with pytest.raises(NotFound):
        await q.get_visible(session, world.me, ev.id)


async def test_hidden_event_not_found_by_id(session, world):
    ev = await add(session, world.mate, "同事私事", "private")
    with pytest.raises(NotFound):
        await q.get_visible(session, world.me, ev.id)


async def test_a13_half_open_interval_boundaries(session, world):
    await add(session, world.me, "10點會議", start=datetime(2026, 10, 22, 2, tzinfo=UTC))  # 台北 10:00–11:00
    at = lambda h, m=0: datetime(2026, 10, 22, h, m, tzinfo=UTC)  # noqa: E731

    assert await titles(session, world.me, start=at(3), end=at(4)) == set()       # 11:00 起：剛好結束，不算
    assert await titles(session, world.me, start=at(1), end=at(2)) == set()       # 到 10:00 止：剛好開始，不算
    assert await titles(session, world.me, start=at(2, 30), end=at(2, 45)) != set()  # 被包含
    assert await titles(session, world.me, start=at(1), end=at(5)) != set()       # 包含整個行程


async def test_a13_all_day_uses_viewer_local_dates(session, world):
    await add(session, world.me, "員工旅遊", all_day=(date(2026, 11, 10), date(2026, 11, 11)))
    # 台北 11/10 00:00 = UTC 11/09 16:00
    day = (datetime(2026, 11, 9, 16, tzinfo=UTC), datetime(2026, 11, 10, 16, tzinfo=UTC))
    prev_day = (datetime(2026, 11, 8, 16, tzinfo=UTC), datetime(2026, 11, 9, 16, tzinfo=UTC))
    assert await titles(session, world.me, start=day[0], end=day[1]) == {("我", "員工旅遊", "detail")}
    assert await titles(session, world.me, start=prev_day[0], end=prev_day[1]) == set()


async def test_member_and_team_filters(session, world):
    await add(session, world.me, "我的會", "team_detail")
    await add(session, world.mate, "同事的會", "team_detail")
    assert await titles(session, world.me, member_ids=[world.mate.id]) == {("王小明", "同事的會", "detail")}
    assert len(await titles(session, world.me, team_id=world.team.id)) == 2


async def test_pagination_reports_has_more(session, world):
    for i in range(3):
        await add(session, world.me, f"會議{i}", start=datetime(2026, 10, 22, i, tzinfo=UTC))
    page = await q.list_visible(session, world.me, start=WEEK[0], end=WEEK[1], limit=2)
    assert [v.title for v in page.items] == ["會議0", "會議1"] and page.has_more
    rest = await q.list_visible(session, world.me, start=WEEK[0], end=WEEK[1], limit=2, offset=2)
    assert [v.title for v in rest.items] == ["會議2"] and not rest.has_more


async def test_range_limits(session, world):
    with pytest.raises(RangeTooLarge):
        await q.list_visible(session, world.me, start=NOW, end=NOW + timedelta(days=91))
    with pytest.raises(RangeTooLarge):
        await q.list_visible(session, world.me, start=NOW, end=NOW)


async def test_inactive_viewer_rejected(session, world):
    world.me.status = "suspended"
    with pytest.raises(MemberInactive):
        await q.list_visible(session, world.me, start=WEEK[0], end=WEEK[1])
