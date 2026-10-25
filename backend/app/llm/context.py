from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ParseContext:
    """解析時的時間基準；`now` 必須帶時區，由後端提供（設計文件 §3.4-1）。"""

    now: datetime
    timezone: str = "Asia/Taipei"
    locale: str = "zh-TW"
    default_duration_minutes: int = 60

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ValueError("ParseContext.now must be timezone-aware")
