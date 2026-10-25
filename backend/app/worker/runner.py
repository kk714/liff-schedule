"""對話 Worker：Reply 截止時間、非同步並行與依使用者序列化（設計文件 §10-4、§10-5，D3）。

    claim(空位數) ──▶ 每筆工作一個 task（Semaphore 限制並行數）
                         │
                  try_acquire(user_id) ──失敗──▶ release(延遲重排)，不處理
                         │成功
       剩餘 Reply 時間 − 安全邊際 ≥ 最低 LLM 預算?
          ├─ 是 ─▶ loading 動畫 ─▶ handle(deadline) ─▶ Reply 結果
          │                          ├─ LLMBudgetExhausted ─▶ Reply「處理較久」─▶ 背景 handle
          │                          └─ 其他 LLMError ─▶ Reply 錯誤說明＋表單連結
          └─ 否 ─▶ Reply「處理較久」（不呼叫 NOVARK）─▶ 背景 handle
       Reply Token 失效 ─▶ 改 Push（額度用盡則放棄，結果仍可在 LIFF 查看）
       背景結果預設不 Push（節省額度），push_late_results=True 才推送
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.llm.errors import LLMBudgetExhausted, LLMError, LLMGuardrailBlocked

from .jobs import ChatJob, HandlerResult
from .ports import ChatHandler, JobSource, Messenger, PushQuotaExceeded, ReplyTokenInvalid, UserLock

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerSettings:
    # 設計文件原訂 8；NOVARK 實測同時 4 個請求即回 429，先用 2，依實測調整
    concurrency: int = 2
    min_llm_budget_seconds: float = 8.0  # 一次 NOVARK 呼叫（含 guardrail）實測 p95 約 8.6 秒以內
    reply_margin_seconds: float = 2.0  # 留給 Reply API 本身的時間
    lock_busy_retry_seconds: float = 0.5
    poll_interval_seconds: float = 0.5
    push_late_results: bool = False
    liff_url: str = ""


def text(message: str) -> dict[str, Any]:
    return {"type": "text", "text": message}


class Worker:
    def __init__(
        self,
        source: JobSource,
        lock: UserLock,
        handler: ChatHandler,
        messenger: Messenger,
        settings: WorkerSettings = WorkerSettings(),
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._lock = lock
        self._handler = handler
        self._messenger = messenger
        self._settings = settings
        self._now = now
        self._monotonic = monotonic
        self._slots = asyncio.Semaphore(settings.concurrency)
        self._in_flight: set[asyncio.Task[None]] = set()

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            claimed = await self.run_once()
            if not claimed:
                try:
                    await asyncio.wait_for(stop.wait(), self._settings.poll_interval_seconds)
                except TimeoutError:
                    pass
        await self.drain()

    async def run_once(self) -> int:
        """依空位數領取工作並啟動，回傳領取筆數；不等待工作完成。"""
        free = self._settings.concurrency - len(self._in_flight)
        if free <= 0:
            return 0
        jobs = await self._source.claim(free)
        for job in jobs:
            task = asyncio.create_task(self._guarded(job))
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)
        return len(jobs)

    async def drain(self) -> None:
        if self._in_flight:
            await asyncio.gather(*self._in_flight, return_exceptions=True)

    async def _guarded(self, job: ChatJob) -> None:
        async with self._slots:
            handle = await self._lock.try_acquire(job.user_id)
            if handle is None:
                await self._source.release(job.job_id, self._settings.lock_busy_retry_seconds)
                return
            async with handle:
                try:
                    await self._run(job)
                except Exception as exc:
                    log.exception("chat job failed", extra={"job_id": job.job_id})
                    try:
                        await self._send(job, [text(self._error_text())], allow_push=False)
                    except Exception:
                        log.warning("error reply failed", extra={"job_id": job.job_id}, exc_info=True)
                    await self._source.fail(job.job_id, f"{type(exc).__name__}: {exc}"[:500])
                    return
                await self._source.complete(job.job_id)

    async def _run(self, job: ChatJob) -> None:
        budget = self._llm_budget(job)
        if budget is None:
            await self._send(job, [text(self._late_text())], allow_push=False)
            await self._finish_in_background(job)
            return

        await self._start_loading(job, budget + self._settings.reply_margin_seconds)
        try:
            result = await self._handler.handle(job, deadline=self._monotonic() + budget)
        except LLMBudgetExhausted:
            await self._send(job, [text(self._late_text())], allow_push=False)
            await self._finish_in_background(job)
            return
        except LLMError as exc:
            await self._send(job, self._llm_error_messages(exc))
            return
        await self._send(job, result.messages)

    def _llm_budget(self, job: ChatJob) -> float | None:
        """可用於 NOVARK 的秒數；不足最低預算時回傳 None。"""
        if job.reply_token is None or job.reply_deadline is None:
            return None
        remaining = (job.reply_deadline - self._now()).total_seconds()
        budget = remaining - self._settings.reply_margin_seconds
        return budget if budget >= self._settings.min_llm_budget_seconds else None

    async def _finish_in_background(self, job: ChatJob) -> None:
        try:
            result = await self._handler.handle(job, deadline=None)
        except LLMError as exc:
            log.warning("background parse failed", extra={"job_id": job.job_id, "code": exc.code})
            if self._settings.push_late_results:
                await self._push(job, self._llm_error_messages(exc))
            return
        if self._settings.push_late_results:
            await self._push(job, result.messages)

    async def _send(self, job: ChatJob, messages: list[dict[str, Any]], *, allow_push: bool = True) -> None:
        if job.reply_token is not None:
            try:
                await self._messenger.reply(job.reply_token, messages)
                return
            except ReplyTokenInvalid:
                log.info("reply token invalid", extra={"job_id": job.job_id})
        if allow_push:
            await self._push(job, messages)

    async def _push(self, job: ChatJob, messages: list[dict[str, Any]]) -> None:
        try:
            await self._messenger.push(
                job.line_user_id, messages, retry_key=str(uuid.uuid5(uuid.NAMESPACE_URL, f"push:{job.job_id}"))
            )
        except PushQuotaExceeded:
            log.warning("push quota exceeded; result stays readable in LIFF", extra={"job_id": job.job_id})

    async def _start_loading(self, job: ChatJob, seconds: float) -> None:
        # LINE 載入動畫接受 5–60 秒、5 的倍數
        rounded = min(60, max(5, 5 * math.ceil(seconds / 5)))
        try:
            await self._messenger.start_loading(job.line_user_id, rounded)
        except Exception:
            log.warning("loading animation failed", extra={"job_id": job.job_id}, exc_info=True)

    def _with_link(self, message: str) -> str:
        return f"{message}\n{self._settings.liff_url}" if self._settings.liff_url else message

    def _late_text(self) -> str:
        return self._with_link("處理較久，完成後可在這裡查看：")

    def _error_text(self) -> str:
        return self._with_link("目前無法處理，請稍後再試，或改用表單建立：")

    def _llm_error_messages(self, exc: LLMError) -> list[dict[str, Any]]:
        if isinstance(exc, LLMGuardrailBlocked):
            if exc.category == "pii":
                return [text(self._with_link("請勿輸入個資。也可以改用表單建立："))]
            return [text(self._with_link("無法處理此要求。也可以改用表單建立："))]
        return [text(self._error_text())]
