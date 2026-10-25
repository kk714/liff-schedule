"""LINE Messaging API：Reply、Push 與聊天載入動畫（設計文件 §10）。"""

from __future__ import annotations

from typing import Any

import httpx

from app.worker.ports import MessengerError, PushQuotaExceeded, ReplyTokenInvalid

API_BASE = "https://api.line.me"


class LineMessenger:
    def __init__(self, channel_access_token: str, client: httpx.AsyncClient, *, timeout: float = 10.0) -> None:
        self._token = channel_access_token
        self._client = client
        self._timeout = timeout

    async def start_loading(self, line_user_id: str, seconds: int) -> None:
        await self._post("/v2/bot/chat/loading/start", {"chatId": line_user_id, "loadingSeconds": seconds})

    async def reply(self, reply_token: str, messages: list[dict[str, Any]]) -> None:
        resp = await self._post("/v2/bot/message/reply", {"replyToken": reply_token, "messages": messages},
                                raise_for_status=False)
        if resp.status_code == 400 and "reply token" in resp.text.lower():
            raise ReplyTokenInvalid(resp.text[:200])
        _raise_for_status(resp)

    async def push(self, line_user_id: str, messages: list[dict[str, Any]], *, retry_key: str) -> None:
        resp = await self._post(
            "/v2/bot/message/push", {"to": line_user_id, "messages": messages},
            headers={"X-Line-Retry-Key": retry_key}, raise_for_status=False,
        )
        if resp.status_code == 409:
            return  # 同一 retry key 已送出過，視為成功
        if resp.status_code == 429 and "monthly limit" in resp.text.lower():
            raise PushQuotaExceeded(resp.text[:200])
        _raise_for_status(resp)

    async def _post(
        self, path: str, body: dict[str, Any], *, headers: dict[str, str] | None = None,
        raise_for_status: bool = True,
    ) -> httpx.Response:
        resp = await self._client.post(
            f"{API_BASE}{path}", json=body, timeout=self._timeout,
            headers={"Authorization": f"Bearer {self._token}", **(headers or {})},
        )
        if raise_for_status:
            _raise_for_status(resp)
        return resp


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        raise MessengerError(f"LINE API {resp.status_code}: {resp.text[:200]}")
