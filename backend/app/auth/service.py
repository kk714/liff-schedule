"""成員綁定與 session（設計文件 §4.2、§4.3，P10）。

    管理員（CLI）建立成員 → 發邀請（單次、72 小時）
    使用者 LIFF：preview(邀請) 顯示姓名 → 本人確認「這是我」→ redeem(邀請, LINE ID token)
        ├─ 邀請已用／撤銷／過期 → InvitationInvalid(410)
        ├─ 這個 LINE 已綁其他成員，或成員已綁其他 LINE → AlreadyBound(409)
        └─ 建立 external_identities、標記邀請已用、寫稽核、發 session
    之後登入：LINE ID token → 已綁定且啟用 → session；未綁定回 bound=false
    撤銷綁定（換手機／換 LINE）：刪除綁定、撤銷所有 session、放棄未完成草稿，再重新發邀請
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, EventDraft, ExternalIdentity, Invitation, Organization, Session, User
from app.services.errors import AlreadyBound, InvitationInvalid, MemberInactive, Unauthorized

from .line_login import LineClaims
from .tokens import hash_token, new_token

INVITATION_TTL = timedelta(hours=72)
SESSION_TTL = timedelta(hours=12)


@dataclass(frozen=True)
class InvitationPreview:
    display_name: str
    organization_name: str
    expires_at: datetime


@dataclass(frozen=True)
class IssuedSession:
    token: str
    user: User
    expires_at: datetime


async def create_invitation(
    s: AsyncSession, member: User, *, now: datetime, created_by: User | None = None,
    ttl: timedelta = INVITATION_TTL,
) -> str:
    """回傳原始邀請 token（只此一次）；同一成員舊的未使用邀請一併撤銷。"""
    await s.execute(
        update(Invitation)
        .where(Invitation.target_user_id == member.id, Invitation.used_at.is_(None), Invitation.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    token = new_token()
    s.add(Invitation(organization_id=member.organization_id, target_user_id=member.id,
                     token_hash=hash_token(token), expires_at=now + ttl,
                     created_by_user_id=created_by.id if created_by else None))
    _audit(s, member.organization_id, created_by, "invitation.create", "user", member.id, now)
    await s.flush()
    return token


async def preview_invitation(s: AsyncSession, token: str, *, now: datetime) -> InvitationPreview:
    invitation, member = await _usable_invitation(s, token, now)
    org = await s.get(Organization, member.organization_id)
    return InvitationPreview(member.display_name, org.name, invitation.expires_at)


async def redeem_invitation(
    s: AsyncSession, token: str, claims: LineClaims, *, provider_scope: str, now: datetime,
) -> IssuedSession:
    invitation, member = await _usable_invitation(s, token, now)
    existing = await s.scalar(select(ExternalIdentity).where(
        ExternalIdentity.provider == "line", ExternalIdentity.subject == claims.sub))
    if existing is not None:
        raise AlreadyBound("this LINE account is already bound")
    if await s.scalar(select(ExternalIdentity).where(
            ExternalIdentity.user_id == member.id, ExternalIdentity.provider == "line")) is not None:
        raise AlreadyBound("member is already bound")
    try:
        async with s.begin_nested():
            s.add(ExternalIdentity(user_id=member.id, provider="line", provider_scope=provider_scope,
                                   subject=claims.sub))
            await s.flush()
    except IntegrityError as exc:  # 唯一約束是最後防線
        raise AlreadyBound("binding conflict") from exc
    invitation.used_at = now
    _audit(s, member.organization_id, member, "identity.bind", "user", member.id, now)
    return await issue_session(s, member, now=now)


async def login_with_line(s: AsyncSession, claims: LineClaims, *, now: datetime) -> IssuedSession | None:
    """已綁定且啟用回傳 session；未綁定回傳 None（前端導向綁定頁，§4.2-5）。"""
    member = await s.scalar(
        select(User).join(ExternalIdentity, ExternalIdentity.user_id == User.id)
        .where(ExternalIdentity.provider == "line", ExternalIdentity.subject == claims.sub)
    )
    if member is None:
        return None
    if member.status != "active":
        raise MemberInactive("member is not active")
    return await issue_session(s, member, now=now)


async def issue_session(s: AsyncSession, member: User, *, now: datetime, ttl: timedelta = SESSION_TTL) -> IssuedSession:
    token = new_token()
    expires = now + ttl
    s.add(Session(user_id=member.id, token_hash=hash_token(token), expires_at=expires))
    await s.flush()
    return IssuedSession(token, member, expires)


async def resolve_session(s: AsyncSession, token: str | None, *, now: datetime) -> User:
    if not token:
        raise Unauthorized("no session")
    row = await s.scalar(select(Session).where(Session.token_hash == hash_token(token)))
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        raise Unauthorized("session invalid or expired")
    member = await s.get(User, row.user_id)
    if member is None or member.status != "active":
        raise Unauthorized("member is not active")  # 停權後舊 session 一律失效（A15）
    return member


async def revoke_session(s: AsyncSession, token: str, *, now: datetime) -> None:
    await s.execute(update(Session).where(Session.token_hash == hash_token(token), Session.revoked_at.is_(None))
                    .values(revoked_at=now))


async def revoke_binding(s: AsyncSession, member: User, *, now: datetime, actor: User | None = None) -> bool:
    """解除 LINE 綁定；回傳是否原本有綁定。"""
    identity = await s.scalar(select(ExternalIdentity).where(
        ExternalIdentity.user_id == member.id, ExternalIdentity.provider == "line"))
    if identity is not None:
        await s.delete(identity)
    await revoke_access(s, member, now=now)
    _audit(s, member.organization_id, actor, "identity.revoke", "user", member.id, now)
    await s.flush()
    return identity is not None


async def revoke_access(s: AsyncSession, member: User, *, now: datetime) -> None:
    """撤銷所有 session，並放棄未完成草稿（舊確認卡隨之失效）；不動綁定。"""
    await s.execute(update(Session).where(Session.user_id == member.id, Session.revoked_at.is_(None))
                    .values(revoked_at=now))
    await s.execute(update(EventDraft).where(
        EventDraft.user_id == member.id, EventDraft.status.in_(("collecting", "awaiting_confirmation"))
    ).values(status="abandoned"))
    await s.flush()


async def _usable_invitation(s: AsyncSession, token: str, now: datetime) -> tuple[Invitation, User]:
    invitation = await s.scalar(select(Invitation).where(Invitation.token_hash == hash_token(token or "")))
    if (invitation is None or invitation.used_at is not None or invitation.revoked_at is not None
            or invitation.expires_at <= now):
        raise InvitationInvalid("invitation is not usable")
    member = await s.get(User, invitation.target_user_id)
    if member is None or member.status != "active":
        raise InvitationInvalid("member is not active")
    return invitation, member


def _audit(s, org_id: uuid.UUID, actor: User | None, action: str, target_type: str,
           target_id: uuid.UUID, now: datetime) -> None:
    s.add(AuditLog(organization_id=org_id, actor_user_id=actor.id if actor else None, action=action,
                   target_type=target_type, target_id=target_id, changed_fields=[], created_at=now))
