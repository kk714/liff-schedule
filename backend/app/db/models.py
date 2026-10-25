"""v1 資料模型（設計文件 §8、§8.1）。外部行事曆匯入相關資料表延後（附錄 A）。

組織一致性在資料庫層強制：users／teams 另有 (organization_id, id) 唯一鍵，
引用成員或團隊的資料表用複合外鍵 (organization_id, user_id)，跨組織的關聯無法寫入。

    organizations ─┬─ users ─┬─ external_identities（LINE 綁定）
                   │         ├─ sessions／invitations／organization_roles
                   │         ├─ events ◀── event_drafts ◀── conversation_sessions.pending_draft_id
                   │         │    └─ event_attendees（受邀成員）
                   │         └─ async_jobs（Worker 佇列，見 app/worker/sqlite_queue.py）
                   └─ teams ─── team_members
    inbound_events（webhook 去重）、message_outbox、idempotency_keys、audit_logs、llm_call_logs
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Timestamps, UUIDPrimaryKey, str_enum, utcnow

VISIBILITY = ("private", "busy_only", "team_detail")


class Organization(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Taipei")
    status: Mapped[str] = mapped_column(str_enum("org_status", "active", "suspended"), default="active")
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)


class User(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "employee_code"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    employee_code: Mapped[str | None] = mapped_column(String(50))
    display_name: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Taipei")
    status: Mapped[str] = mapped_column(str_enum("user_status", "active", "suspended", "left"), default="active")
    default_visibility: Mapped[str] = mapped_column(str_enum("user_default_visibility", *VISIBILITY),
                                                    default="team_detail")  # 2026-10-25：預設讓團隊看見行程


class ExternalIdentity(UUIDPrimaryKey, Timestamps, Base):
    """LINE 綁定。撤銷綁定（§4.3）時刪除此列，並寫入 audit_logs。"""

    __tablename__ = "external_identities"
    __table_args__ = (
        UniqueConstraint("provider", "provider_scope", "subject"),
        UniqueConstraint("user_id", "provider"),  # 一個成員只綁一個 LINE 身分
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(str_enum("identity_provider", "line"))
    provider_scope: Mapped[str] = mapped_column(String(64))  # LINE Provider ID
    subject: Mapped[str] = mapped_column(String(64))  # 驗證後的 LINE userId（sub）


class Team(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "name"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(str_enum("team_status", "active", "archived"), default="active")


class TeamMember(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "team_members"
    __table_args__ = (
        UniqueConstraint("team_id", "user_id"),
        ForeignKeyConstraint(["organization_id", "team_id"], ["teams.organization_id", "teams.id"],
                             ondelete="CASCADE"),
        ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"],
                             ondelete="CASCADE"),
        Index(None, "organization_id", "user_id"),
    )

    organization_id: Mapped[uuid.UUID]
    team_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]


class OrganizationRole(UUIDPrimaryKey, Timestamps, Base):
    """管理權限；與行程分享權限分離（§7.1）。"""

    __tablename__ = "organization_roles"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", "role"),
        ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"],
                             ondelete="CASCADE"),
    )

    organization_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]
    role: Mapped[str] = mapped_column(str_enum("org_role", "admin"))


class Invitation(UUIDPrimaryKey, Timestamps, Base):
    """單次使用、72 小時到期的綁定邀請（§4.3）；只存 token 雜湊。"""

    __tablename__ = "invitations"
    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "target_user_id"], ["users.organization_id", "users.id"],
                             ondelete="CASCADE"),
    )

    organization_id: Mapped[uuid.UUID]
    target_user_id: Mapped[uuid.UUID]
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class Session(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


class Event(UUIDPrimaryKey, Timestamps, Base):
    """正式行程。非全天存 timestamptz＋IANA 時區；全天存 [start_date, end_date)（§6、§8.1）。"""

    __tablename__ = "events"
    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "owner_user_id"], ["users.organization_id", "users.id"]),
        CheckConstraint(
            "(all_day = false AND start_at IS NOT NULL AND end_at IS NOT NULL AND end_at > start_at"
            " AND start_date IS NULL AND end_date IS NULL)"
            " OR (all_day = true AND start_date IS NOT NULL AND end_date IS NOT NULL AND end_date > start_date"
            " AND start_at IS NULL AND end_at IS NULL)",
            name="time_shape",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        Index(None, "organization_id", "owner_user_id", "start_at"),
        Index(None, "organization_id", "owner_user_id", "start_date"),
    )

    organization_id: Mapped[uuid.UUID]
    owner_user_id: Mapped[uuid.UUID]
    title: Mapped[str] = mapped_column(String(100))
    start_at: Mapped[datetime | None]
    end_at: Mapped[datetime | None]
    timezone: Mapped[str] = mapped_column(String(64))
    all_day: Mapped[bool] = mapped_column(default=False)
    start_date: Mapped[date | None]
    end_date: Mapped[date | None]
    location: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(str_enum("event_visibility", *VISIBILITY))
    status: Mapped[str] = mapped_column(str_enum("event_status", "active", "cancelled"), default="active")
    # 列舉保留擴充值（附錄 A 的外部來源）
    source: Mapped[str] = mapped_column(str_enum("event_source", "conversation", "manual"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    # 每週重複時，同一次建立的所有行程共用一個 series_id（取消時可選「這次與之後全部」）
    series_id: Mapped[uuid.UUID | None] = mapped_column(index=True)


class EventAttendee(UUIDPrimaryKey, Timestamps, Base):
    """受邀參與者。行程只有一筆（擁有者為主辦人），參與者看到的是同一筆，主辦人修改會同步。"""

    __tablename__ = "event_attendees"
    __table_args__ = (
        UniqueConstraint("event_id", "user_id"),
        ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"],
                             ondelete="CASCADE"),
        Index(None, "user_id", "status"),
    )

    organization_id: Mapped[uuid.UUID]
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID]
    status: Mapped[str] = mapped_column(str_enum("attendee_status", "accepted", "declined"), default="accepted")
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class EventDraft(UUIDPrimaryKey, Timestamps, Base):
    """草稿狀態機見 §5.3；確認時以 draft_version＋列鎖確保只提交一次。"""

    __tablename__ = "event_drafts"
    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"],
                             ondelete="CASCADE"),
        CheckConstraint("draft_version >= 1", name="draft_version_positive"),
        CheckConstraint("intent = 'create' OR event_id IS NOT NULL", name="target_event_required"),
        Index(None, "user_id", "status"),
    )

    organization_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]
    intent: Mapped[str] = mapped_column(str_enum("draft_intent", "create", "update", "cancel"))
    event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("events.id"))
    base_event_version: Mapped[int | None]
    draft_version: Mapped[int] = mapped_column(Integer, default=1)
    payload_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(
        str_enum("draft_status", "collecting", "awaiting_confirmation", "committed", "abandoned", "expired"),
        default="collecting",
    )
    expires_at: Mapped[datetime]
    committed_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("events.id"))


class ConversationSession(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "conversation_sessions"
    __table_args__ = (
        UniqueConstraint("user_id", "channel"),
        ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"],
                             ondelete="CASCADE"),
    )

    organization_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]
    channel: Mapped[str] = mapped_column(str_enum("conversation_channel", "line"), default="line")
    pending_draft_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("event_drafts.id", ondelete="SET NULL")
    )
    context_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    expires_at: Mapped[datetime]


class InboundEvent(UUIDPrimaryKey, Base):
    """LINE webhook 事件；以 channel_id＋webhook_event_id 去重（§10-2）。"""

    __tablename__ = "inbound_events"
    __table_args__ = (UniqueConstraint("channel_id", "webhook_event_id"),)

    channel_id: Mapped[str] = mapped_column(String(64))
    webhook_event_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]]
    processing_status: Mapped[str] = mapped_column(
        str_enum("inbound_status", "received", "enqueued", "ignored", "failed"), default="received"
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    received_at: Mapped[datetime] = mapped_column(default=utcnow)


class AsyncJob(UUIDPrimaryKey, Timestamps, Base):
    """Worker 佇列（app/worker/sqlite_queue.py）。"""

    __tablename__ = "async_jobs"
    __table_args__ = (
        Index(None, "type", "status", "available_at", "received_at"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )

    type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(
        str_enum("job_status", "queued", "running", "done", "failed"), default="queued"
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    line_user_id: Mapped[str | None] = mapped_column(String(64))
    message_text: Mapped[str | None] = mapped_column(Text)
    reply_token: Mapped[str | None] = mapped_column(String(128))
    reply_deadline: Mapped[datetime | None]
    received_at: Mapped[datetime] = mapped_column(default=utcnow)
    inbound_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inbound_events.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(default=utcnow)
    locked_until: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)


class MessageOutbox(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "message_outbox"

    recipient_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(str_enum("outbox_type", "reply", "push"))
    payload: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(
        str_enum("outbox_status", "pending", "sent", "failed", "skipped"), default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    deduplication_key: Mapped[str] = mapped_column(String(128), unique=True)
    last_error: Mapped[str | None] = mapped_column(Text)


class IdempotencyKey(UUIDPrimaryKey, Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "route", "key"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    route: Mapped[str] = mapped_column(String(128))
    key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(128))
    response_json: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]


class AuditLog(UUIDPrimaryKey, Base):
    """只記欄位名稱，不存標題、地點、備註原文；僅系統維運可讀（P8）。"""

    __tablename__ = "audit_logs"
    __table_args__ = (Index(None, "organization_id", "created_at"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[uuid.UUID | None]
    changed_fields: Mapped[list[Any]] = mapped_column(default=list)
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class LLMCallLog(UUIDPrimaryKey, Base):
    """每次 NOVARK HTTP 嘗試一列，對應 app.llm.CallRecord；不存提示詞與原文（§3.1.3）。"""

    __tablename__ = "llm_call_logs"
    __table_args__ = (Index(None, "request_id"), Index(None, "created_at"))

    request_id: Mapped[str] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    task: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(32))
    attempt: Mapped[int] = mapped_column(Integer)
    agent: Mapped[str] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(str_enum("llm_call_status", "ok", "error"))
    http_status: Mapped[int | None]
    duration_ms: Mapped[int | None]
    token_usage: Mapped[int | None]
    binding_snapshot_id: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    novark_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
