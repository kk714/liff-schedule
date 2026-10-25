"""LINE ID token 驗證（設計文件 §4.2）：交給 LINE 官方 verify 端點，並檢查 audience、issuer 與有效期。

不信任前端傳來的 userId、姓名或角色；本系統只使用驗證後的 sub。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"
ISSUER = "https://access.line.me"


class InvalidIdToken(Exception):
    pass


@dataclass(frozen=True)
class LineClaims:
    sub: str
    name: str | None = None


class LineIdTokenVerifier:
    def __init__(self, channel_id: str, client: httpx.AsyncClient, *, timeout: float = 10.0) -> None:
        self._channel_id = channel_id
        self._client = client
        self._timeout = timeout

    async def verify(self, id_token: str) -> LineClaims:
        if not id_token or not self._channel_id:
            raise InvalidIdToken("missing id_token or LINE_LOGIN_CHANNEL_ID")
        try:
            resp = await self._client.post(
                VERIFY_URL, data={"id_token": id_token, "client_id": self._channel_id}, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise InvalidIdToken(f"verify request failed: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise InvalidIdToken(f"LINE rejected id_token ({resp.status_code})")
        claims = resp.json()
        # LINE 端點已驗證簽章與 client_id；這裡再檢查一次，避免設定錯誤時誤收其他 channel 的 token
        if claims.get("aud") != self._channel_id or claims.get("iss") != ISSUER:
            raise InvalidIdToken("unexpected audience or issuer")
        if int(claims.get("exp", 0)) <= time.time():
            raise InvalidIdToken("id_token expired")
        if not claims.get("sub"):
            raise InvalidIdToken("missing sub")
        return LineClaims(sub=claims["sub"], name=claims.get("name"))
