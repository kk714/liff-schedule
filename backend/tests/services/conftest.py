from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import Organization, Team, TeamMember, User

NOW = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)  # 台北 10:00（週三）


@dataclass
class World:
    org: Organization
    me: User          # 查詢者
    mate: User        # 與我同團隊
    stranger: User    # 同公司、無共同團隊
    outsider: User    # 別家公司
    team: Team
    other_team: Team


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


@pytest.fixture
async def world(session: AsyncSession) -> World:
    org, other_org = Organization(name="公司"), Organization(name="別家公司")
    session.add_all([org, other_org])
    await session.flush()
    me = User(organization_id=org.id, display_name="我")
    mate = User(organization_id=org.id, display_name="王小明")
    stranger = User(organization_id=org.id, display_name="陌生同事")
    outsider = User(organization_id=other_org.id, display_name="外人")
    session.add_all([me, mate, stranger, outsider])
    await session.flush()
    team = Team(organization_id=org.id, name="業務部")
    other_team = Team(organization_id=org.id, name="研發部")
    session.add_all([team, other_team])
    await session.flush()
    session.add_all([
        TeamMember(organization_id=org.id, team_id=team.id, user_id=me.id),
        TeamMember(organization_id=org.id, team_id=team.id, user_id=mate.id),
        TeamMember(organization_id=org.id, team_id=other_team.id, user_id=stranger.id),
    ])
    await session.flush()
    return World(org, me, mate, stranger, outsider, team, other_team)
