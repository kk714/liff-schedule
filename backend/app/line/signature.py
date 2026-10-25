"""LINE webhook 簽章驗證：以 Channel Secret 對「未修改的原始 body」做 HMAC-SHA256（設計文件 §10-1）。"""

from __future__ import annotations

import base64
import hashlib
import hmac


def sign(body: bytes, channel_secret: str) -> str:
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify(body: bytes, signature: str | None, channel_secret: str) -> bool:
    if not signature or not channel_secret:
        return False
    return hmac.compare_digest(sign(body, channel_secret), signature)
