"""NL 解析評測的案例格式與評分（設計文件 §14.1）。

案例（cases.jsonl 一行一筆）：
    {"id": "a01", "cat": "create_basic", "msg": "...",
     "now": "2026-10-21T10:00:00+08:00",          # 選填，預設 DEFAULT_NOW
     "draft": {...},                               # 選填，pending_draft
     "expect": {"intent": "create" | ["create", "unsupported"],
                "allow_guardrail": false,          # 選填：被 guardrail 攔下也算通過
                "checks": {...}}}                  # 選填：欄位檢查

checks 的鍵為 `<區塊>.<欄位>` 或 `missing_fields`／`ambiguities`，值的寫法：
    "2026-11-01T14:00:00" / null / true    完全相等（時間以 ISO 字串比較）
    {"contains": ["國網中心"]}              字串需包含每一個片段
    {"includes": ["王小明"]}                清單需包含每一個元素（字串清單時以包含比對）
    {"excludes": ["end_local"]}             清單不可包含
    {"nonempty": true} / {"empty": true}    清單是否為空

意圖正確率 = intent 正確的案例數 ÷ 全部案例（錯誤與逾時算失敗）。
欄位完全正確率 = 所有 checks 都通過的案例數 ÷ 有 checks 的案例（intent 錯則欄位也算失敗）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

DEFAULT_NOW = "2026-10-21T10:00:00+08:00"  # 週三；本週 10/19–10/25，下週 10/26–11/1
INTENT_THRESHOLD = 0.95
FIELD_THRESHOLD = 0.90

VALID_INTENTS = {"create", "update", "cancel", "query", "clarify", "unsupported"}
VALID_SECTIONS = {"event", "target", "query"}
LIST_KEYS = {"missing_fields", "ambiguities", "date_flags"}  # date_flags 來自後端日期規則
MATCHER_KEYS = {"contains", "includes", "excludes", "nonempty", "empty"}


@dataclass
class Outcome:
    """一次解析的結果：parsed（成功）、guardrail（被攔下）或 error（其他錯誤）。"""

    kind: str  # "parsed" | "guardrail" | "error"
    parsed: dict[str, Any] | None = None
    error_code: str | None = None
    error_detail: str | None = None
    latency_ms: int | None = None
    token_usage: int | None = None


@dataclass
class CaseScore:
    id: str
    cat: str
    intent_ok: bool
    fields_ok: bool | None  # None 表示此案例沒有欄位檢查
    failures: list[str] = field(default_factory=list)


def validate_case(case: dict[str, Any]) -> list[str]:
    """回傳案例格式錯誤；空清單代表合法。"""
    problems: list[str] = []
    for key in ("id", "cat", "msg", "expect"):
        if key not in case:
            problems.append(f"missing {key}")
    expect = case.get("expect", {})
    intents = expect.get("intent")
    intents = intents if isinstance(intents, list) else [intents]
    if not intents or any(i not in VALID_INTENTS for i in intents):
        problems.append(f"bad intent {expect.get('intent')!r}")
    for key, want in expect.get("checks", {}).items():
        if key in LIST_KEYS:
            if not isinstance(want, dict) or not set(want) <= MATCHER_KEYS:
                problems.append(f"bad matcher for {key}")
            continue
        section, _, name = key.partition(".")
        if section not in VALID_SECTIONS or not name:
            problems.append(f"bad check key {key}")
        if isinstance(want, dict) and not set(want) <= MATCHER_KEYS:
            problems.append(f"bad matcher for {key}")
    if "now" in case:
        try:
            if datetime.fromisoformat(case["now"]).tzinfo is None:
                problems.append("now must have offset")
        except ValueError:
            problems.append("bad now")
    return problems


def score_case(case: dict[str, Any], outcome: Outcome) -> CaseScore:
    expect = case["expect"]
    checks: dict[str, Any] = expect.get("checks", {})
    has_checks = bool(checks)
    intents = expect["intent"] if isinstance(expect["intent"], list) else [expect["intent"]]

    if outcome.kind == "guardrail":
        ok = bool(expect.get("allow_guardrail"))
        return CaseScore(
            case["id"], case["cat"], ok, (ok if has_checks else None),
            [] if ok else ["blocked by guardrail"],
        )
    if outcome.kind != "parsed" or outcome.parsed is None:
        return CaseScore(
            case["id"], case["cat"], False, (False if has_checks else None),
            [f"error: {outcome.error_code}"],
        )

    parsed = outcome.parsed
    failures: list[str] = []
    intent_ok = parsed.get("intent") in intents
    if not intent_ok:
        failures.append(f"intent: want {intents}, got {parsed.get('intent')!r}")

    if not has_checks:
        return CaseScore(case["id"], case["cat"], intent_ok, None, failures)

    field_failures = [msg for key, want in checks.items() if (msg := _check(parsed, key, want))]
    failures.extend(field_failures)
    return CaseScore(case["id"], case["cat"], intent_ok, intent_ok and not field_failures, failures)


def _lookup(parsed: dict[str, Any], key: str) -> Any:
    if key in LIST_KEYS:
        return parsed.get(key) or []
    section, _, name = key.partition(".")
    block = parsed.get(section)
    if not isinstance(block, dict):
        return None
    return block.get(name)


def _norm(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).replace(tzinfo=None).isoformat(timespec="seconds")
        except ValueError:
            return value
    return value


def _check(parsed: dict[str, Any], key: str, want: Any) -> str | None:
    got = _lookup(parsed, key)
    if not isinstance(want, dict):
        if _norm(got) == _norm(want):
            return None
        return f"{key}: want {want!r}, got {got!r}"

    if "contains" in want:
        text = got if isinstance(got, str) else ""
        missing = [s for s in want["contains"] if s.lower() not in text.lower()]
        if got is None or missing:
            return f"{key}: want contains {want['contains']}, got {got!r}"
    items = got if isinstance(got, list) else []
    if "includes" in want:
        for needle in want["includes"]:
            if not any(needle.lower() in str(item).lower() for item in items):
                return f"{key}: want includes {needle!r}, got {got!r}"
    if "excludes" in want:
        for needle in want["excludes"]:
            if needle in items:
                return f"{key}: want excludes {needle!r}, got {got!r}"
    if want.get("nonempty") and not items:
        return f"{key}: want nonempty, got {got!r}"
    if want.get("empty") and items:
        return f"{key}: want empty, got {got!r}"
    return None


def summarize(scores: list[CaseScore], outcomes: list[Outcome]) -> dict[str, Any]:
    total = len(scores)
    with_checks = [s for s in scores if s.fields_ok is not None]
    intent_acc = sum(s.intent_ok for s in scores) / total if total else 0.0
    field_acc = (
        sum(bool(s.fields_ok) for s in with_checks) / len(with_checks) if with_checks else 1.0
    )
    latencies = sorted(o.latency_ms for o in outcomes if o.latency_ms is not None)

    by_cat: dict[str, dict[str, Any]] = {}
    for s in scores:
        c = by_cat.setdefault(s.cat, {"n": 0, "intent_ok": 0, "fields_n": 0, "fields_ok": 0})
        c["n"] += 1
        c["intent_ok"] += s.intent_ok
        if s.fields_ok is not None:
            c["fields_n"] += 1
            c["fields_ok"] += s.fields_ok

    return {
        "cases": total,
        "intent_accuracy": round(intent_acc, 4),
        "field_accuracy": round(field_acc, 4),
        "passed": intent_acc >= INTENT_THRESHOLD and field_acc >= FIELD_THRESHOLD,
        "thresholds": {"intent": INTENT_THRESHOLD, "field": FIELD_THRESHOLD},
        "errors": sum(o.kind == "error" for o in outcomes),
        "guardrail_blocks": sum(o.kind == "guardrail" for o in outcomes),
        "latency_ms_p50": _percentile(latencies, 0.50),
        "latency_ms_p95": _percentile(latencies, 0.95),
        "token_usage_total": sum(o.token_usage or 0 for o in outcomes),
        "by_category": by_cat,
    }


def _percentile(sorted_values: list[int], q: float) -> int | None:
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, round(q * (len(sorted_values) - 1)))
    return sorted_values[idx]
