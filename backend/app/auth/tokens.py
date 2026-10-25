"""隨機 token 與雜湊：資料庫只存 SHA-256，原始 token 只交給使用者一次。"""

from __future__ import annotations

import hashlib
import secrets


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
