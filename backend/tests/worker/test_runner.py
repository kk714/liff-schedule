"""Worker 主流程測試（記憶體佇列與鎖；PostgreSQL 版需環境驗收，見 §14.2）。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.llm.errors import LLMBudgetExhausted, LLMGuardrailBlocked, LLMUpstream
from app.worker import ChatJob, HandlerResult, Worker, WorkerSettings
from app.worker.memory import InProcessUserLock, MemoryJobSource
from app.worker.ports import PushQuotaExceeded, ReplyTokenInvalid

T0 = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)
LIFF = "https://liff.line.me/test"


class Clock:
    def __init__(self) -> None:
        self.wall = T0
        self.mono = 100.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


class FakeMessenger:
    def __init__(self, *, reply_error: Exception | None = None, push_error: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.reply_error = reply_error
        self.push_error = push_error

    async def start_loading(self, line_user_id, seconds):
        self.calls.append(("loading", line_user_id, seconds))

    async def reply(self, reply_token, messages):
        self.calls.append(("reply", reply_token, [m["text"] for m in messages]))
        if self.reply_error:
            raise self.reply_error

    async def push(self, line_user_id, messages, *, retry_key):
        self.calls.append(("push", line_user_id, [m["text"] for m in messages]))
        if self.push_error:
            raise self.push_error

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


class FakeHandler:
    def __init__(self, *outcomes, delay: float = 0.0) -> None:
        self.outcomes = list(outcomes)
        self.deadlines: list[float | None] = []
        self.delay = delay
        self.events: list[tuple[str, str]] = []

    async def handle(self, job, *, deadline):
        self.deadlines.append(deadline)
        self.events.append(("start", job.job_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        self.events.append(("end", job.job_id))
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        return HandlerResult([{"type": "text", "text": f"result:{job.job_id}"}])


def job(job_id="j1", user="00000000-0000-0000-0000-000000000001", *, remaining=30.0, token=True, offset=0):
    received = T0 + timedelta(milliseconds=offset)
    return ChatJob(
        job_id=job_id, user_id=user, line_user_id=f"U{user[-1]}", message_text="明天10點開會",
        received_at=received,
        reply_token=f"rt-{job_id}" if token else None,
        reply_deadline=T0 + timedelta(seconds=remaining) if token else None,
    )


def make_worker(handler, messenger, *, source=None, lock=None, clock=None, **settings):
    clock = clock or Clock()
    source = source or MemoryJobSource(now=clock.now)
    worker = Worker(
        source, lock or InProcessUserLock(), handler, messenger,
        WorkerSettings(liff_url=LIFF, **settings), now=clock.now, monotonic=clock.monotonic,
    )
    return worker, source, clock


async def run_all(worker: Worker) -> None:
    """反覆領取直到佇列沒有可執行的工作。"""
    while True:
        await worker.drain()
        if not await worker.run_once():
            break
    await worker.drain()


async def test_enough_budget_shows_loading_then_replies_result():
    handler, messenger = FakeHandler(), FakeMessenger()
    worker, source, clock = make_worker(handler, messenger)
    source.enqueue(job(remaining=30))

    await run_all(worker)

    assert messenger.calls[0] == ("loading", "U1", 30)
    assert messenger.calls[1] == ("reply", "rt-j1", ["result:j1"])
    assert handler.deadlines == [clock.mono + 28.0]  # 剩餘 30 秒 − Reply 邊際 2 秒
    assert source.status("j1") == "done"


async def test_insufficient_budget_replies_late_notice_without_calling_llm_first():
    handler, messenger = FakeHandler(), FakeMessenger()
    worker, source, _ = make_worker(handler, messenger)
    source.enqueue(job(remaining=9))  # 9 − 2 = 7 < 最低預算 8

    await run_all(worker)

    assert messenger.calls == [("reply", "rt-j1", [f"處理較久，完成後可在這裡查看：\n{LIFF}"])]
    assert handler.deadlines == [None]  # 回覆後才在背景解析
    assert source.status("j1") == "done"


async def test_budget_exhausted_mid_call_falls_back_then_finishes_in_background():
    handler, messenger = FakeHandler(LLMBudgetExhausted("late"), "ok"), FakeMessenger()
    worker, source, _ = make_worker(handler, messenger)
    source.enqueue(job(remaining=30))

    await run_all(worker)

    assert messenger.kinds() == ["loading", "reply"]
    assert messenger.calls[1][2][0].startswith("處理較久")
    assert handler.deadlines[1] is None
    assert "push" not in messenger.kinds()  # 預設不推送背景結果
    assert source.status("j1") == "done"


async def test_late_result_pushed_when_enabled():
    handler, messenger = FakeHandler(), FakeMessenger()
    worker, source, _ = make_worker(handler, messenger, push_late_results=True)
    source.enqueue(job(remaining=5))

    await run_all(worker)

    assert messenger.kinds() == ["reply", "push"]
    assert messenger.calls[1][2] == ["result:j1"]


async def test_job_without_reply_token_runs_in_background_silently():
    handler, messenger = FakeHandler(), FakeMessenger()
    worker, source, _ = make_worker(handler, messenger)
    source.enqueue(job(token=False))

    await run_all(worker)

    assert messenger.calls == []
    assert handler.deadlines == [None]


async def test_guardrail_block_replies_with_form_link():
    handler = FakeHandler(LLMGuardrailBlocked("x", category="prompt_injection"))
    messenger = FakeMessenger()
    worker, source, _ = make_worker(handler, messenger)
    source.enqueue(job())

    await run_all(worker)

    assert messenger.calls[-1] == ("reply", "rt-j1", [f"無法處理此要求。也可以改用表單建立：\n{LIFF}"])
    assert source.status("j1") == "done"


async def test_llm_error_replies_error_message():
    handler, messenger = FakeHandler(LLMUpstream("down")), FakeMessenger()
    worker, source, _ = make_worker(handler, messenger)
    source.enqueue(job())

    await run_all(worker)

    assert messenger.calls[-1][2][0].startswith("目前無法處理")
    assert source.status("j1") == "done"


async def test_unexpected_exception_marks_job_failed_and_apologizes():
    handler, messenger = FakeHandler(RuntimeError("bug")), FakeMessenger()
    worker, source, _ = make_worker(handler, messenger)
    source.enqueue(job())

    await run_all(worker)

    assert source.status("j1") == "failed"
    assert "RuntimeError: bug" in source.entry("j1").last_error
    assert messenger.calls[-1][2][0].startswith("目前無法處理")


async def test_invalid_reply_token_falls_back_to_push():
    handler = FakeHandler()
    messenger = FakeMessenger(reply_error=ReplyTokenInvalid("expired"))
    worker, source, _ = make_worker(handler, messenger)
    source.enqueue(job())

    await run_all(worker)

    assert messenger.kinds() == ["loading", "reply", "push"]
    assert messenger.calls[-1][2] == ["result:j1"]


async def test_push_quota_exceeded_does_not_fail_job():
    handler = FakeHandler()
    messenger = FakeMessenger(reply_error=ReplyTokenInvalid("expired"), push_error=PushQuotaExceeded("limit"))
    worker, source, _ = make_worker(handler, messenger)
    source.enqueue(job())

    await run_all(worker)

    assert source.status("j1") == "done"


async def test_late_notice_is_never_pushed():
    handler = FakeHandler()
    messenger = FakeMessenger(reply_error=ReplyTokenInvalid("expired"))
    worker, source, _ = make_worker(handler, messenger)
    source.enqueue(job(remaining=3))

    await run_all(worker)

    assert messenger.kinds() == ["reply"]  # 「處理較久」不值得耗用 Push 額度


@pytest.mark.parametrize("remaining,expected", [(30, 30), (12, 15), (60, 60), (200, 60)])
async def test_loading_seconds_rounded_to_line_limits(remaining, expected):
    handler, messenger = FakeHandler(), FakeMessenger()
    worker, source, _ = make_worker(handler, messenger)
    source.enqueue(job(remaining=remaining))

    await run_all(worker)

    assert messenger.calls[0] == ("loading", "U1", expected)


async def test_same_user_jobs_run_in_order_one_at_a_time():
    handler, messenger = FakeHandler(delay=0.02), FakeMessenger()
    worker, source, clock = make_worker(handler, messenger, concurrency=2, lock_busy_retry_seconds=0)
    source.enqueue(job("j1", offset=0))
    source.enqueue(job("j2", offset=1))

    await run_all(worker)

    assert handler.events == [("start", "j1"), ("end", "j1"), ("start", "j2"), ("end", "j2")]
    assert source.status("j1") == source.status("j2") == "done"


async def test_lock_busy_releases_job_back_to_queue():
    handler, messenger = FakeHandler(delay=0.02), FakeMessenger()
    worker, source, clock = make_worker(handler, messenger, concurrency=2, lock_busy_retry_seconds=5)
    source.enqueue(job("j1", offset=0))
    source.enqueue(job("j2", offset=1))

    await worker.run_once()
    await worker.drain()

    assert source.status("j1") == "done"
    assert source.status("j2") == "queued"
    assert source.entry("j2").attempts == 0  # 鎖被占用不算一次嘗試
    assert await source.claim(10) == []  # 5 秒內不重領
    clock.wall += timedelta(seconds=5)
    assert [j.job_id for j in await source.claim(10)] == ["j2"]


async def test_different_users_run_concurrently_within_limit():
    handler, messenger = FakeHandler(delay=0.05), FakeMessenger()
    worker, source, _ = make_worker(handler, messenger, concurrency=2)
    users = [f"00000000-0000-0000-0000-00000000000{i}" for i in range(1, 5)]
    for i, u in enumerate(users):
        source.enqueue(job(f"j{i}", user=u, offset=i))

    await worker.run_once()
    await asyncio.sleep(0.01)
    starts = [e for e in handler.events if e[0] == "start"]
    assert len(starts) == 2  # 並行上限 2：同時只領 2 筆
    await run_all(worker)
    assert all(source.status(f"j{i}") == "done" for i in range(4))


async def test_run_forever_stops_cleanly():
    handler, messenger = FakeHandler(), FakeMessenger()
    worker, source, _ = make_worker(handler, messenger, poll_interval_seconds=0.01)
    source.enqueue(job())
    stop = asyncio.Event()

    task = asyncio.create_task(worker.run_forever(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1)

    assert source.status("j1") == "done"
