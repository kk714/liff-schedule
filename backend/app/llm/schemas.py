"""NOVARK 回應外層與行程解析結果的資料模型。

外層回應（NovarkRunResponse）允許未知欄位，平台實測會多回 `usage_calls`；
嚴格 Schema 只套用在 `output.json` 的業務內容（ParsedSchedule），見設計文件 §3.1.1、§3.3。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Intent = Literal["create", "update", "cancel", "query", "clarify", "unsupported"]
Visibility = Literal["private", "busy_only", "team_detail"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParsedEvent(_Strict):
    title: str | None = Field(default=None, max_length=100)
    start_local: datetime | None = None
    end_local: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    all_day: bool = False
    location: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=1000)
    visibility: Visibility | None = None


class ParsedTarget(_Strict):
    """update／cancel 的搜尋條件；不含資料庫 ID。"""

    on_date: date | None = None
    start_local: datetime | None = None
    title_keyword: str | None = Field(default=None, max_length=100)


class ParsedQuery(_Strict):
    person_names: list[str] = Field(default_factory=list, max_length=10)
    range_start: date | None = None
    range_end: date | None = None
    keyword: str | None = Field(default=None, max_length=100)


class ParsedSchedule(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    intent: Intent
    event: ParsedEvent | None = None
    target: ParsedTarget | None = None
    query: ParsedQuery | None = None
    missing_fields: list[str] = Field(default_factory=list, max_length=20)
    ambiguities: list[str] = Field(default_factory=list, max_length=20)


class NovarkGuardrail(BaseModel):
    model_config = ConfigDict(extra="allow")

    category: str | None = None
    action: str | None = None


class NovarkOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: str = ""
    json_: Any = Field(default=None, alias="json")
    parse_error: str | None = None
    response_format: str | None = None
    notes: list[str] = Field(default_factory=list)
    guardrail: NovarkGuardrail | None = None
    input_text: str | None = None


class NovarkUsageCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str | None = None
    tool: str | None = None
    total_tokens: int | None = None


class NovarkRunResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    success: bool
    agent: str | None = None
    trace_id: str | None = None
    duration_ms: int | None = None
    output: NovarkOutput | None = None
    error: str | None = None
    error_code: str | None = None
    token_usage: int | None = None
    binding_snapshot_id: str | None = None
    usage_calls: list[NovarkUsageCall] = Field(default_factory=list)
