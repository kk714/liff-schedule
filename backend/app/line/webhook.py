"""LINE webhook（設計文件 §10-1～§10-2）。

    驗簽（原始 body）─失敗→ 401
    逐一事件：inbound_events 以 (destination, webhookEventId) 去重 ─重複→ 略過
      ├─ 非一對一（group／room）→ ignored（§4.1）
      ├─ 未綁定的 LINE 使用者 → 排一筆工作回覆綁定說明
      ├─ 文字 → chat 工作（帶 reply_token 與 reply_deadline）
      ├─ postback → chat 工作（確認／放棄／選擇候選）
      └─ 其他訊息（貼圖、圖片…）→ chat 工作，由處理器回固定短句（D7）
    全部寫入同一交易後才回 200；寫入失敗回 500，讓 LINE 重送（不可先回 2xx 再寫入）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AsyncJob, ExternalIdentity, InboundEvent, User

from .signature import verify

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/line/webhook")
async def line_webhook(request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    body = await request.body()
    if not verify(body, request.headers.get("x-line-signature"), settings.line_channel_secret):
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc

    received_at = datetime.now(timezone.utc)
    async with request.app.state.sessions() as session:
        await ingest(session, data, received_at=received_at, reply_window=settings.reply_window_seconds)
        await session.commit()
    return {"status": "ok"}  # events 為空的驗證請求也回 200


async def ingest(session: AsyncSession, data: dict[str, Any], *, received_at: datetime, reply_window: float) -> int:
    """寫入事件與工作，回傳新增的工作數；呼叫端負責 commit。"""
    destination = data.get("destination", "")
    created = 0
    for event in data.get("events", []):
        event_id = event.get("webhookEventId")
        if not event_id:
            continue
        inbound = InboundEvent(channel_id=destination, webhook_event_id=event_id, payload=event,
                               received_at=received_at)
        try:
            async with session.begin_nested():
                session.add(inbound)
                await session.flush()
        except IntegrityError:
            log.info("duplicate webhook event", extra={"webhook_event_id": event_id})
            continue  # LINE 重送：同一事件只處理一次

        job = await _job_for(session, event, received_at, reply_window)
        if job is None:
            inbound.processing_status = "ignored"
            continue
        job.inbound_event_id = inbound.id
        session.add(job)
        inbound.processing_status = "enqueued"
        created += 1
    await session.flush()
    return created


async def _job_for(session: AsyncSession, event: dict[str, Any], received_at: datetime,
                   reply_window: float) -> AsyncJob | None:
    source = event.get("source", {})
    if source.get("type") != "user" or event.get("type") not in ("message", "postback"):
        return None
    line_user_id = source.get("userId")
    if not line_user_id:
        return None

    user = await session.scalar(
        select(User).join(ExternalIdentity, ExternalIdentity.user_id == User.id)
        .where(ExternalIdentity.provider == "line", ExternalIdentity.subject == line_user_id)
    )
    if event["type"] == "postback":
        payload = {"kind": "postback", "postback": event.get("postback", {}).get("data", "")}
        text = None
    elif event.get("message", {}).get("type") == "text":
        payload, text = {"kind": "text"}, event["message"].get("text", "")[:1000]
    else:
        payload, text = {"kind": "non_text"}, None
    if user is None:
        payload = {"kind": "unbound"}  # Webhook 的 userId 必須對應啟用成員（§4.3）

    reply_token = event.get("replyToken")
    return AsyncJob(
        type="chat", user_id=user.id if user else None, line_user_id=line_user_id,
        message_text=text, payload=payload, received_at=received_at, available_at=received_at,
        reply_token=reply_token,
        reply_deadline=received_at + timedelta(seconds=reply_window) if reply_token else None,
    )
