"""草稿 API（設計文件 §9）：LIFF 的草稿確認／表單修改與表單建立。全部只能操作本人的草稿。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.db.models import Event, User
from app.services import DraftFields, DraftInput, DraftService

from .auth import csrf_protected, current_user
from .schemas import draft_out, event_out

router = APIRouter(prefix="/api")
drafts = DraftService()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class VersionIn(BaseModel):
    draft_version: int = Field(ge=1)


class PatchIn(VersionIn):
    fields: DraftFields


class FormDraftIn(BaseModel):
    intent: Literal["create", "update", "cancel"]
    fields: DraftFields = Field(default_factory=DraftFields)
    event_id: uuid.UUID | None = None


async def _event_for(s, draft) -> Event | None:
    return await s.get(Event, draft.event_id) if draft.event_id else None


@router.get("/drafts/{draft_id}")
async def get_draft(draft_id: uuid.UUID, request: Request, user: User = Depends(current_user)) -> dict:
    async with request.app.state.sessions() as s:
        draft = await drafts.get(s, user, draft_id)
        out = draft_out(draft, await _event_for(s, draft), ZoneInfo(user.timezone))
        await s.rollback()
    return out


@router.patch("/drafts/{draft_id}", dependencies=[Depends(csrf_protected)])
async def patch_draft(draft_id: uuid.UUID, body: PatchIn, request: Request,
                      user: User = Depends(current_user)) -> dict:
    async with request.app.state.sessions() as s:
        cleared = {k for k in body.fields.model_fields_set if getattr(body.fields, k) is None}
        draft = await drafts.update(s, user, draft_id, body.draft_version, body.fields, now=_now(), clear=cleared)
        out = draft_out(draft, await _event_for(s, draft), ZoneInfo(user.timezone))
        await s.commit()
    return out


@router.post("/drafts/{draft_id}/confirm", dependencies=[Depends(csrf_protected)])
async def confirm_draft(draft_id: uuid.UUID, body: VersionIn, request: Request,
                        user: User = Depends(current_user)) -> dict:
    """同一版本重送回傳原結果（連點、網路重試皆安全），版本過時回 409、過期回 410。"""
    async with request.app.state.sessions() as s:
        result = await drafts.confirm(s, user, draft_id, body.draft_version, now=_now(),
                                      request_id=request.headers.get("x-request-id"))
        out = {"intent": result.intent, "already_committed": result.already_committed, "affected": result.affected,
               "event": event_out(result.event, ZoneInfo(user.timezone))}
        await s.commit()
    return out


@router.post("/drafts/{draft_id}/abandon", dependencies=[Depends(csrf_protected)])
async def abandon_draft(draft_id: uuid.UUID, body: VersionIn, request: Request,
                        user: User = Depends(current_user)) -> dict:
    async with request.app.state.sessions() as s:
        await drafts.abandon(s, user, draft_id, body.draft_version, now=_now())
        await s.commit()
    return {"status": "abandoned"}


@router.post("/events/drafts", dependencies=[Depends(csrf_protected)])
async def form_draft(body: FormDraftIn, request: Request, user: User = Depends(current_user)) -> dict:
    """由 LIFF 表單建立新增／修改／取消草稿；仍須另外確認才寫入（§5）。"""
    async with request.app.state.sessions() as s:
        draft = await drafts.create(s, user, DraftInput(body.intent, body.fields, event_id=body.event_id), now=_now())
        out = draft_out(draft, await _event_for(s, draft), ZoneInfo(user.timezone))
        await s.commit()
    return out
