"""執行期設定，全部來自環境變數；機密不寫進程式或 image（設計文件 §3.1、§12）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Settings:
    line_channel_secret: str = field(default="", repr=False)
    line_channel_access_token: str = field(default="", repr=False)
    liff_url: str = ""
    line_login_channel_id: str = ""  # LIFF 所屬 LINE Login channel，用於 ID token 的 audience
    line_provider_id: str = "default"  # Login 與 Messaging API 須在同一 Provider（§4.1）
    cookie_secure: bool = True
    worker_enabled: bool = True
    worker_concurrency: int = 2
    reply_window_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            line_channel_secret=os.environ.get("LINE_CHANNEL_SECRET", ""),
            line_channel_access_token=os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""),
            liff_url=os.environ.get("LIFF_URL", ""),
            line_login_channel_id=os.environ.get("LINE_LOGIN_CHANNEL_ID", ""),
            line_provider_id=os.environ.get("LINE_PROVIDER_ID", "default"),
            cookie_secure=_bool("COOKIE_SECURE", True),
            worker_enabled=_bool("WORKER_ENABLED", True),
            worker_concurrency=int(os.environ.get("WORKER_CONCURRENCY", "2")),
            reply_window_seconds=float(os.environ.get("REPLY_WINDOW_SECONDS", "30")),
        )
