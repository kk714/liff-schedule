"""NOVARK llm_agent Adapter（設計文件 §3.1.1–3.1.3）。

    parse_schedule()
      │
      ├─ _run("parse:{rid}") ──┐  每次 _run 內部：
      │                         │    剩餘預算? ─否→ LLMBudgetExhausted
      │                         │    POST /api/agents/{agent}/run
      │                         │    429/502/503/504/網路 → 退避重試（同 idempotency_key）
      │                         │    其他 4xx/success=false → 對應錯誤，不重試
      ├─ guardrail? ─是→ LLMGuardrailBlocked
      ├─ text 空白? ─是→ _run("parse-retry:{rid}") 一次，仍空 → LLMEmptyOutput
      ├─ JSON/Schema 失敗? ─是→ _run("repair:{rid}") 一次，仍失敗 → LLMMalformedOutput
      └─ offset 正規化（D5）→ ParseResult

idempotency_key 依用途區分：NOVARK 對「同 key＋同 inputs」直接回傳上次結果，
所以空白重試與修復必須換 key，網路重試則必須沿用同 key。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from .context import ParseContext
from .errors import (
    LLMBudgetExhausted,
    LLMConfigError,
    LLMEmptyOutput,
    LLMError,
    LLMGuardrailBlocked,
    LLMMalformedOutput,
    LLMProviderUnavailable,
    LLMRateLimited,
    LLMTokenBudget,
    LLMUpstream,
)
from .prompts import build_repair_payload, build_system_prompt, build_user_payload
from .schemas import NovarkRunResponse, ParsedSchedule

_RETRYABLE_5XX = {502, 503, 504}
_CONFIG_4XX = {400, 401, 402, 403, 404}


@dataclass(frozen=True)
class NovarkSettings:
    service_token: str = field(repr=False)
    base_url: str = "https://core.novark.cloud"
    agent: str = "llm_agent"
    input_guard: bool = True
    temperature: float = 0.1
    max_tokens: int = 512
    http_timeout_seconds: float = 130.0
    max_retries: int = 2
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 4.0

    @classmethod
    def from_env(cls) -> NovarkSettings:
        token = os.environ.get("NOVARK_SERVICE_TOKEN", "")
        if not token:
            raise LLMConfigError("NOVARK_SERVICE_TOKEN is not set")
        return cls(
            service_token=token,
            base_url=os.environ.get("NOVARK_BASE_URL", cls.base_url),
            agent=os.environ.get("NOVARK_AGENT", cls.agent),
            input_guard=os.environ.get("NOVARK_INPUT_GUARD", "true").lower() == "true",
            temperature=float(os.environ.get("NOVARK_TEMPERATURE", cls.temperature)),
            max_tokens=int(os.environ.get("NOVARK_MAX_TOKENS", cls.max_tokens)),
            http_timeout_seconds=float(
                os.environ.get("NOVARK_HTTP_TIMEOUT_SECONDS", cls.http_timeout_seconds)
            ),
            max_retries=int(os.environ.get("NOVARK_MAX_RETRIES", cls.max_retries)),
        )

    @property
    def run_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/agents/{self.agent}/run"


@dataclass(frozen=True)
class CallRecord:
    """單次 HTTP 嘗試的紀錄，供寫入 llm_call_logs；不含提示詞或原文。"""

    request_id: str
    purpose: str
    attempt: int
    status: str  # "ok" | "error"
    http_status: int | None = None
    trace_id: str | None = None
    duration_ms: int | None = None
    token_usage: int | None = None
    binding_snapshot_id: str | None = None
    model: str | None = None
    error_code: str | None = None
    novark_error_code: str | None = None


@dataclass
class ParseResult:
    parsed: ParsedSchedule
    trace_id: str | None
    calls: list[CallRecord]


OnCall = Callable[[CallRecord], Awaitable[None] | None]


class NovarkAdapter:
    def __init__(
        self,
        settings: NovarkSettings,
        client: httpx.AsyncClient,
        *,
        on_call: OnCall | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._settings = settings
        self._client = client
        self._on_call = on_call
        self._clock = clock
        self._sleep = sleep
        self._rand = rand

    async def parse_schedule(
        self,
        message: str,
        ctx: ParseContext,
        *,
        request_id: str,
        pending_draft: dict[str, Any] | None = None,
        deadline: float | None = None,
    ) -> ParseResult:
        """解析一則使用者訊息。

        `deadline` 為 `clock()` 時間軸上的截止點（聊天路徑依 Reply 截止時間計算）；
        None 表示只受 HTTP 逾時限制（背景路徑）。
        """
        calls: list[CallRecord] = []
        system_prompt = build_system_prompt(ctx)
        payload = build_user_payload(message, pending_draft)

        resp = await self._run(
            system_prompt, payload, request_id, "parse", deadline, calls
        )
        _raise_if_guardrail(resp)
        output = resp.output

        if output is None or not output.text.strip():
            resp = await self._run(
                system_prompt, payload, request_id, "parse-retry", deadline, calls
            )
            _raise_if_guardrail(resp)
            output = resp.output
            if output is None or not output.text.strip():
                raise LLMEmptyOutput("empty model output", trace_id=resp.trace_id)

        try:
            parsed = _validate(resp)
        except _InvalidOutput as first:
            repair = build_repair_payload(payload, output.text, str(first))
            resp = await self._run(
                system_prompt, repair, request_id, "repair", deadline, calls
            )
            _raise_if_guardrail(resp)
            try:
                parsed = _validate(resp)
            except _InvalidOutput as second:
                raise LLMMalformedOutput(
                    f"model output invalid after repair: {second}",
                    trace_id=resp.trace_id,
                ) from second

        return ParseResult(
            parsed=normalize_offsets(parsed, ctx),
            trace_id=resp.trace_id,
            calls=calls,
        )

    async def _run(
        self,
        system_prompt: str,
        prompt: str,
        request_id: str,
        purpose: str,
        deadline: float | None,
        calls: list[CallRecord],
    ) -> NovarkRunResponse:
        s = self._settings
        body = {
            "inputs": {
                "input_guard": s.input_guard,
                "system_prompt": system_prompt,
                "prompt": prompt,
                "response_format": "json",
                "temperature": s.temperature,
                "max_tokens": s.max_tokens,
            },
            "idempotency_key": f"{purpose}:{request_id}",
        }
        headers = {
            "Authorization": f"Bearer {s.service_token}",
            "Content-Type": "application/json",
            "X-Trace-Id": request_id,
        }

        last_error: LLMError | None = None
        for attempt in range(s.max_retries + 1):
            timeout = s.http_timeout_seconds
            if deadline is not None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise LLMBudgetExhausted(
                        "time budget exhausted before NOVARK call"
                    ) from last_error
                timeout = min(timeout, remaining)

            try:
                resp = await self._post(body, headers, timeout)
                result = self._interpret(resp)
            except LLMError as exc:
                await self._record(
                    calls, request_id, purpose, attempt, error=exc
                )
                if not exc.retryable or attempt >= s.max_retries:
                    raise
                last_error = exc
                delay = self._backoff(attempt)
                if isinstance(exc, LLMRateLimited) and exc.retry_after is not None:
                    delay = max(delay, exc.retry_after)
                if deadline is not None and self._clock() + delay >= deadline:
                    raise LLMBudgetExhausted(
                        "time budget exhausted while retrying NOVARK"
                    ) from exc
                await self._sleep(delay)
                continue

            await self._record(calls, request_id, purpose, attempt, result=result)
            return result

        raise AssertionError("unreachable")  # pragma: no cover

    async def _post(
        self, body: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> httpx.Response:
        try:
            return await self._client.post(
                self._settings.run_url, json=body, headers=headers, timeout=timeout
            )
        except httpx.TimeoutException as exc:
            raise LLMUpstream("NOVARK request timed out") from exc
        except httpx.TransportError as exc:
            raise LLMUpstream(f"NOVARK transport error: {type(exc).__name__}") from exc

    def _interpret(self, resp: httpx.Response) -> NovarkRunResponse:
        status = resp.status_code
        if status != 200:
            detail_code = _detail_error_code(resp)
            kwargs = {"http_status": status, "novark_error_code": detail_code}
            if status == 429:
                raise LLMRateLimited(
                    "NOVARK rate limited", retry_after=_retry_after(resp), **kwargs
                )
            if status == 422:
                raise LLMTokenBudget("NOVARK token budget exceeded", **kwargs)
            if status in _RETRYABLE_5XX:
                raise LLMUpstream(f"NOVARK HTTP {status}", **kwargs)
            if status in _CONFIG_4XX:
                raise LLMConfigError(f"NOVARK HTTP {status}", **kwargs)
            raise LLMUpstream(f"NOVARK HTTP {status}", retryable=False, **kwargs)

        try:
            run = NovarkRunResponse.model_validate(resp.json())
        except (ValueError, ValidationError) as exc:
            raise LLMUpstream(
                "NOVARK returned an unreadable envelope", retryable=False, http_status=200
            ) from exc

        if not run.success:
            kwargs = {
                "trace_id": run.trace_id,
                "novark_error_code": run.error_code,
                "http_status": 200,
            }
            if run.error_code == "provider_unavailable":
                raise LLMProviderUnavailable(run.error or "provider unavailable", **kwargs)
            if run.error_code == "token_budget_exceeded":
                raise LLMTokenBudget(run.error or "token budget exceeded", **kwargs)
            raise LLMUpstream(
                run.error or "NOVARK run failed", retryable=False, **kwargs
            )
        return run

    def _backoff(self, attempt: int) -> float:
        s = self._settings
        base = min(s.backoff_max_seconds, s.backoff_base_seconds * (2**attempt))
        return base * (0.5 + self._rand() / 2)

    async def _record(
        self,
        calls: list[CallRecord],
        request_id: str,
        purpose: str,
        attempt: int,
        *,
        result: NovarkRunResponse | None = None,
        error: LLMError | None = None,
    ) -> None:
        if result is not None:
            model = next((u.model for u in result.usage_calls if u.tool == "llm_chat"), None)
            record = CallRecord(
                request_id=request_id,
                purpose=purpose,
                attempt=attempt,
                status="ok",
                http_status=200,
                trace_id=result.trace_id,
                duration_ms=result.duration_ms,
                token_usage=result.token_usage,
                binding_snapshot_id=result.binding_snapshot_id,
                model=model,
            )
        else:
            assert error is not None
            record = CallRecord(
                request_id=request_id,
                purpose=purpose,
                attempt=attempt,
                status="error",
                http_status=error.http_status,
                trace_id=error.trace_id,
                error_code=error.code,
                novark_error_code=error.novark_error_code,
            )
        calls.append(record)
        if self._on_call is not None:
            maybe = self._on_call(record)
            if inspect.isawaitable(maybe):
                await maybe


class _InvalidOutput(Exception):
    pass


def _raise_if_guardrail(resp: NovarkRunResponse) -> None:
    if resp.output is not None and resp.output.guardrail is not None:
        raise LLMGuardrailBlocked(
            "input blocked by NOVARK guardrail",
            category=resp.output.guardrail.category,
            trace_id=resp.trace_id,
        )


def _validate(resp: NovarkRunResponse) -> ParsedSchedule:
    output = resp.output
    if output is None:
        raise _InvalidOutput("missing output")
    if output.parse_error:
        raise _InvalidOutput(f"parse_error: {output.parse_error}")
    data = output.json_
    if data is None:
        try:
            data = json.loads(output.text)
        except ValueError as exc:
            raise _InvalidOutput(f"not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _InvalidOutput("JSON root is not an object")
    try:
        return ParsedSchedule.model_validate(data)
    except ValidationError as exc:
        raise _InvalidOutput(f"schema: {exc.error_count()} error(s): {exc.errors()[:3]}") from exc


def _retry_after(resp: httpx.Response) -> float | None:
    """只處理秒數格式；平台實測回 `retry-after: 1`。"""
    try:
        return max(0.0, float(resp.headers["retry-after"]))
    except (KeyError, ValueError):
        return None


def _detail_error_code(resp: httpx.Response) -> str | None:
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        return None
    if isinstance(detail, dict):
        code = detail.get("error_code")
        return code if isinstance(code, str) else None
    return None


def normalize_offsets(parsed: ParsedSchedule, ctx: ParseContext) -> ParsedSchedule:
    """D5：offset 與使用者時區在該時刻相同就去掉；不同就清空欄位並列入 ambiguities。"""
    tz = ZoneInfo(ctx.timezone)
    ambiguities = list(parsed.ambiguities)

    def fix(value: datetime | None, name: str) -> datetime | None:
        if value is None or value.tzinfo is None:
            return value
        naive = value.replace(tzinfo=None)
        if value.utcoffset() == naive.replace(tzinfo=tz).utcoffset():
            return naive
        marker = f"{name}:timezone_offset_mismatch"
        if marker not in ambiguities:
            ambiguities.append(marker)
        return None

    updates: dict[str, Any] = {}
    if parsed.event is not None:
        updates["event"] = parsed.event.model_copy(
            update={
                "start_local": fix(parsed.event.start_local, "event.start_local"),
                "end_local": fix(parsed.event.end_local, "event.end_local"),
            }
        )
    if parsed.target is not None:
        updates["target"] = parsed.target.model_copy(
            update={"start_local": fix(parsed.target.start_local, "target.start_local")}
        )
    updates["ambiguities"] = ambiguities
    return parsed.model_copy(update=updates)
