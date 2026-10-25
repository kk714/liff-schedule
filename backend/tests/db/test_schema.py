"""資料模型與 migration 測試（SQLite，D8）。

PostgreSQL 專屬行為（列鎖、advisory lock、SKIP LOCKED、timestamptz 時區語意）不在此驗證，
須於 PostgreSQL 環境驗收（設計文件 §14.2、T4）；這裡另以離線模式確認 migration 產生的 PostgreSQL DDL。
"""

from __future__ import annotations

import io
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import (
    AsyncJob,
    Event,
    EventDraft,
    ExternalIdentity,
    IdempotencyKey,
    InboundEvent,
    Organization,
    Team,
    TeamMember,
    User,
)

BACKEND = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)


def alembic_config(url: str | None = None, connection=None, sql_buffer=None) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"), stdout=io.StringIO(), output_buffer=sql_buffer)
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    if url:
        cfg.set_main_option("sqlalchemy.url", url)
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


@pytest.fixture
def engine():
    eng = create_engine("sqlite://")

    @event.listens_for(eng, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    yield eng
    eng.dispose()


@pytest.fixture
def migrated(engine):
    """以 migration（不是 create_all）建立 schema，確保測到的是實際上線的 DDL。"""
    with engine.begin() as conn:
        command.upgrade(alembic_config(connection=conn), "head")
    return engine


def test_migration_matches_models(migrated):
    with migrated.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []


def test_downgrade_removes_everything(migrated):
    with migrated.begin() as conn:
        command.downgrade(alembic_config(connection=conn), "base")
    assert set(inspect(migrated).get_table_names()) <= {"alembic_version"}


def test_postgres_offline_sql_uses_native_types():
    out = io.StringIO()
    command.upgrade(alembic_config("postgresql+asyncpg://u:p@localhost/db", sql_buffer=out), "head", sql=True)
    sql = out.getvalue()
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "JSONB" in sql
    assert "UUID" in sql
    assert "CONSTRAINT ck_events_time_shape CHECK" in sql
    assert "FOREIGN KEY(organization_id, owner_user_id) REFERENCES users (organization_id, id)" in sql


def test_async_jobs_has_columns_used_by_worker_sql(migrated):
    cols = {c["name"] for c in inspect(migrated).get_columns("async_jobs")}
    # app/worker/postgres.py 的 SQL 依賴這些欄位
    assert {"id", "type", "status", "user_id", "line_user_id", "message_text", "reply_token",
            "reply_deadline", "received_at", "attempts", "available_at", "locked_until",
            "last_error"} <= cols


# --- 約束行為 -----------------------------------------------------------------

def seed(s: Session) -> tuple[Organization, User]:
    org = Organization(name="公司")
    s.add(org)
    s.flush()
    user = User(organization_id=org.id, display_name="王小明")
    s.add(user)
    s.flush()
    return org, user


def timed_event(org, user, **kw) -> Event:
    base = dict(organization_id=org.id, owner_user_id=user.id, title="國網中心拜訪",
                start_at=NOW, end_at=NOW + timedelta(hours=1), timezone="Asia/Taipei",
                visibility="busy_only", source="conversation")
    return Event(**{**base, **kw})


def assert_rejected(s: Session, obj) -> None:
    s.add(obj)
    with pytest.raises(IntegrityError):
        s.flush()
    s.rollback()


def test_defaults_applied(migrated):
    with Session(migrated) as s:
        org, user = seed(s)
        ev = timed_event(org, user)
        s.add(ev)
        s.flush()
        assert (ev.status, ev.version, ev.all_day) == ("active", 1, False)
        assert user.default_visibility == "busy_only" and org.timezone == "Asia/Taipei"


def test_timed_event_requires_end_after_start(migrated):
    with Session(migrated) as s:
        org, user = seed(s)
        assert_rejected(s, timed_event(org, user, end_at=NOW))


def test_timed_event_cannot_carry_dates(migrated):
    with Session(migrated) as s:
        org, user = seed(s)
        assert_rejected(s, timed_event(org, user, start_date=date(2026, 10, 21), end_date=date(2026, 10, 22)))


def test_all_day_event_shape(migrated):
    with Session(migrated) as s:
        org, user = seed(s)
        ok = timed_event(org, user, all_day=True, start_at=None, end_at=None,
                         start_date=date(2026, 11, 10), end_date=date(2026, 11, 11))
        s.add(ok)
        s.flush()
        assert_rejected(s, timed_event(org, user, all_day=True, start_at=None, end_at=None,
                                       start_date=date(2026, 11, 10), end_date=date(2026, 11, 10)))


def test_invalid_visibility_rejected(migrated):
    with Session(migrated) as s:
        org, user = seed(s)
        assert_rejected(s, timed_event(org, user, visibility="public"))


def test_cross_organization_owner_rejected(migrated):
    with Session(migrated) as s:
        _, user = seed(s)
        other = Organization(name="別家公司")
        s.add(other)
        s.flush()
        # 持有正確的 user UUID，但組織不同 → 複合外鍵拒絕（§8.1、A09）
        assert_rejected(s, timed_event(other, user))


def test_team_member_must_share_organization(migrated):
    with Session(migrated) as s:
        org, user = seed(s)
        other = Organization(name="別家公司")
        s.add(other)
        s.flush()
        team = Team(organization_id=other.id, name="業務部")
        s.add(team)
        s.flush()
        assert_rejected(s, TeamMember(organization_id=other.id, team_id=team.id, user_id=user.id))


def test_team_member_unique(migrated):
    with Session(migrated) as s:
        org, user = seed(s)
        team = Team(organization_id=org.id, name="業務部")
        s.add(team)
        s.flush()
        s.add(TeamMember(organization_id=org.id, team_id=team.id, user_id=user.id))
        s.flush()
        assert_rejected(s, TeamMember(organization_id=org.id, team_id=team.id, user_id=user.id))


def test_line_identity_unique_both_ways(migrated):
    with Session(migrated) as s:
        org, user = seed(s)
        s.add(ExternalIdentity(user_id=user.id, provider="line", provider_scope="P1", subject="U123"))
        s.flush()
        second = User(organization_id=org.id, display_name="李經理")
        s.add(second)
        s.flush()
        # 同一個 LINE userId 不能綁第二個成員
        assert_rejected(s, ExternalIdentity(user_id=second.id, provider="line", provider_scope="P1", subject="U123"))
        org, user = seed(s)
        s.add(ExternalIdentity(user_id=user.id, provider="line", provider_scope="P1", subject="U1"))
        s.flush()
        # 同一個成員不能綁第二個 LINE
        assert_rejected(s, ExternalIdentity(user_id=user.id, provider="line", provider_scope="P1", subject="U2"))


def test_webhook_event_dedup(migrated):
    with Session(migrated) as s:
        s.add(InboundEvent(channel_id="C1", webhook_event_id="E1", payload={}))
        s.flush()
        assert_rejected(s, InboundEvent(channel_id="C1", webhook_event_id="E1", payload={}))


def test_idempotency_key_unique_per_user_route(migrated):
    with Session(migrated) as s:
        _, user = seed(s)
        exp = NOW + timedelta(days=1)
        s.add(IdempotencyKey(user_id=user.id, route="/api/drafts/x/confirm", key="k", request_hash="h", expires_at=exp))
        s.flush()
        assert_rejected(s, IdempotencyKey(user_id=user.id, route="/api/drafts/x/confirm", key="k",
                                          request_hash="h2", expires_at=exp))


def test_update_draft_requires_target_event(migrated):
    with Session(migrated) as s:
        org, user = seed(s)
        assert_rejected(s, EventDraft(organization_id=org.id, user_id=user.id, intent="update",
                                      expires_at=NOW + timedelta(minutes=30)))


def test_async_job_defaults(migrated):
    with Session(migrated) as s:
        _, user = seed(s)
        job = AsyncJob(type="chat", user_id=user.id, line_user_id="U1", message_text="明天10點開會",
                       reply_token="rt", reply_deadline=NOW + timedelta(seconds=30))
        s.add(job)
        s.flush()
        assert (job.status, job.attempts) == ("queued", 0)
        assert isinstance(job.id, uuid.UUID)
