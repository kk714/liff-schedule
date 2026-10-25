"""Worker 依賴的介面；正式環境用 PostgreSQL／LINE 實作，測試用記憶體實作。"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from .jobs import ChatJob, HandlerResult


class JobSource(Protocol):
    async def claim(self, limit: int) -> list[ChatJob]:
        """領取最多 limit 筆可執行的工作，依 received_at 由舊到新。"""

    async def complete(self, job_id: str) -> None: ...

    async def fail(self, job_id: str, error: str) -> None: ...

    async def release(self, job_id: str, delay_seconds: float) -> None:
        """未處理就放回佇列（例如同一使用者的鎖被占用），不計入嘗試次數。"""


class UserLock(Protocol):
    async def try_acquire(self, user_id: str) -> AbstractAsyncContextManager[None] | None:
        """取得該使用者的處理權；被占用時回傳 None。"""


class ChatHandler(Protocol):
    async def handle(self, job: ChatJob, *, deadline: float | None) -> HandlerResult:
        """解析訊息並產生草稿／回覆內容。

        deadline 為 monotonic 時間軸上的截止點，傳給 NovarkAdapter；None 表示背景執行。
        預算用完時拋出 LLMBudgetExhausted。
        """


class MessengerError(Exception):
    pass


class ReplyTokenInvalid(MessengerError):
    """Reply Token 已過期或已使用。"""


class PushQuotaExceeded(MessengerError):
    """官方帳號本月訊息額度用盡。"""


class Messenger(Protocol):
    async def start_loading(self, line_user_id: str, seconds: int) -> None: ...

    async def reply(self, reply_token: str, messages: list[dict[str, Any]]) -> None: ...

    async def push(self, line_user_id: str, messages: list[dict[str, Any]], *, retry_key: str) -> None: ...
