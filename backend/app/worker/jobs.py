from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

# LINE 官方文件未保證 Reply Token 的確切有效時間；先取保守值，依實測調整（設計文件 §10-5）。
DEFAULT_REPLY_WINDOW_SECONDS = 30.0


@dataclass(frozen=True)
class ChatJob:
    """一則待處理的 LINE 一對一文字訊息。

    reply_token／reply_deadline 在 webhook 收到事件時寫入；背景續跑的工作兩者皆為 None。
    """

    job_id: str
    user_id: str
    line_user_id: str
    message_text: str
    received_at: datetime
    reply_token: str | None = None
    reply_deadline: datetime | None = None


@dataclass
class HandlerResult:
    """處理結果：要回給使用者的 LINE 訊息物件（text／flex）。"""

    messages: list[dict[str, Any]] = field(default_factory=list)


def compute_reply_deadline(
    received_at: datetime, window_seconds: float = DEFAULT_REPLY_WINDOW_SECONDS
) -> datetime:
    if received_at.tzinfo is None:
        raise ValueError("received_at must be timezone-aware")
    return received_at + timedelta(seconds=window_seconds)
