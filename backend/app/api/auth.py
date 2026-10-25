"""登入、綁定與本人資訊 API（設計文件 §4.2、§4.3、§9）。

Session 存在 HttpOnly＋Secure＋SameSite=Lax 的 `sid` Cookie；變更操作另以 double-submit 驗證 CSRF：
登入時發一個前端讀得到的 `csrf` Cookie，變更請求必須在 `X-CSRF-Token` header 帶回同一值。
登入與兌換本身以 LINE ID token 驗證身分，不需要 CSRF。
"""

from __future__ import annotations

import hmac
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.auth.line_login import InvalidIdToken
from app.auth.service import (
    IssuedSession,
    login_with_line,
    preview_invitation,
    redeem_invitation,
    resolve_session,
    revoke_session,
)
from app.auth.tokens import new_token
from app.db.models import Team, TeamMember, User
from app.services.errors import CsrfFailed, Unauthorized

router = APIRouter(prefix="/api")
SESSION_COOKIE = "sid"
CSRF_COOKIE = "csrf"
CSRF_HEADER = "x-csrf-token"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LineLoginIn(BaseModel):
    id_token: str = Field(min_length=1, max_length=4096)


class InvitationIn(BaseModel):
    token: str = Field(min_length=1, max_length=200)


class RedeemIn(InvitationIn):
    id_token: str = Field(min_length=1, max_length=4096)


def _user_out(user: User) -> dict:
    return {"id": str(user.id), "display_name": user.display_name, "timezone": user.timezone,
            "default_visibility": user.default_visibility}


def _set_session(response: Response, request: Request, issued: IssuedSession) -> None:
    secure = request.app.state.settings.cookie_secure
    max_age = int((issued.expires_at - _now()).total_seconds())
    response.set_cookie(SESSION_COOKIE, issued.token, max_age=max_age, httponly=True, secure=secure,
                        samesite="lax", path="/")
    response.set_cookie(CSRF_COOKIE, new_token(), max_age=max_age, httponly=False, secure=secure,
                        samesite="lax", path="/")


async def _claims(request: Request, id_token: str):
    try:
        return await request.app.state.line_verifier.verify(id_token)
    except InvalidIdToken as exc:
        raise Unauthorized("invalid LINE id_token") from exc


async def current_user(request: Request) -> User:
    async with request.app.state.sessions() as s:
        user = await resolve_session(s, request.cookies.get(SESSION_COOKIE), now=_now())
        s.expunge(user)  # rollback 會讓仍在 session 中的物件過期；先卸下，端點才能讀取欄位
        await s.rollback()
    return user


async def csrf_protected(request: Request) -> None:
    cookie, header = request.cookies.get(CSRF_COOKIE), request.headers.get(CSRF_HEADER)
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise CsrfFailed("missing or mismatched CSRF token")


@router.post("/auth/line")
async def login(body: LineLoginIn, request: Request, response: Response) -> dict:
    claims = await _claims(request, body.id_token)
    async with request.app.state.sessions() as s:
        issued = await login_with_line(s, claims, now=_now())
        await s.commit()
    if issued is None:
        return {"bound": False}
    _set_session(response, request, issued)
    return {"bound": True, "user": _user_out(issued.user)}


@router.post("/invitations/preview")
async def invitation_preview(body: InvitationIn, request: Request) -> dict:
    async with request.app.state.sessions() as s:
        p = await preview_invitation(s, body.token, now=_now())
        await s.rollback()
    return {"display_name": p.display_name, "organization": p.organization_name,
            "expires_at": p.expires_at.isoformat()}


@router.post("/invitations/redeem")
async def invitation_redeem(body: RedeemIn, request: Request, response: Response) -> dict:
    claims = await _claims(request, body.id_token)  # 先在交易外向 LINE 驗證
    async with request.app.state.sessions() as s:
        issued = await redeem_invitation(s, body.token, claims,
                                         provider_scope=request.app.state.settings.line_provider_id, now=_now())
        await s.commit()
    _set_session(response, request, issued)
    return {"bound": True, "user": _user_out(issued.user)}


@router.post("/auth/logout", dependencies=[Depends(csrf_protected)])
async def logout(request: Request, response: Response, user: User = Depends(current_user)) -> dict:
    async with request.app.state.sessions() as s:
        await revoke_session(s, request.cookies[SESSION_COOKIE], now=_now())
        await s.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"status": "ok"}


@router.get("/me")
async def me(request: Request, user: User = Depends(current_user)) -> dict:
    async with request.app.state.sessions() as s:
        teams = (await s.scalars(
            select(Team).join(TeamMember, TeamMember.team_id == Team.id)
            .where(TeamMember.user_id == user.id, Team.status == "active").order_by(Team.name)
        )).all()
        team_out = [{"id": str(t.id), "name": t.name} for t in teams]
        await s.rollback()
    return {"user": _user_out(user), "teams": team_out}
