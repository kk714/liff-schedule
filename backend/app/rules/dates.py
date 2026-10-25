"""由後端判斷的日期規則（設計文件 §6，P7 與 D6）。

模型只照字面解析：沒寫年份一律用今年、「這週X」一律取本週日期。
是否推定為明年、是否要追問，由這裡決定，不交給模型。

    原文有明確「月/日」且沒寫年份，日期又已過？
      ├─ 明年同日在 6 個月內 → 改成明年，flags 加 year_inferred_next_year（確認卡標「明年」）
      └─ 否則               → ambiguities 加 year_unspecified_past（追問年份）
    原文是「這週X」且該日已過？
      └─ ambiguities 加 this_week_day_passed（追問是否指下週X）

只套用在 intent=create 的 event；修改／取消的 target 可以是過去的行程。
"""

from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app.llm.context import ParseContext
from app.llm.schemas import ParsedSchedule

YEAR_INFERRED = "year_inferred_next_year"
YEAR_UNSPECIFIED = "year_unspecified_past"
THIS_WEEK_PASSED = "this_week_day_passed"

_CN_DIGITS = "0-9０-９一二三四五六七八九十"
_EXPLICIT_YEAR = re.compile(r"(?:19|20)\d{2}\s*[/\-.年]|明年|今年|去年|後年|后年")
_SLASH_MD = re.compile(r"(?<!\d)\d{1,2}\s*/\s*\d{1,2}(?!\s*/)")
_MONTH_MD = re.compile(rf"(?<![個个下上這这本])([{_CN_DIGITS}]{{1,3}})\s*月\s*[{_CN_DIGITS}]{{1,3}}\s*[日號号]")
_THIS_WEEK_DAY = re.compile(r"(?:這|这|本)(?:個|个)?(?:週|周|星期|禮拜|礼拜)([一二三四五六日天])")
_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


@dataclass
class DateRuleResult:
    parsed: ParsedSchedule
    flags: list[str] = field(default_factory=list)


def apply_date_rules(parsed: ParsedSchedule, message: str, ctx: ParseContext) -> DateRuleResult:
    event = parsed.event
    if parsed.intent != "create" or event is None or event.start_local is None:
        return DateRuleResult(parsed)

    today = ctx.now.date()
    start = event.start_local
    end = event.end_local
    ambiguities = list(parsed.ambiguities)
    flags: list[str] = []

    if has_explicit_month_day(message) and not _EXPLICIT_YEAR.search(message) and start.date() < today:
        candidate = _with_year(start, start.year + 1)
        if candidate.date() <= add_months(today, 6):
            shift = candidate - start
            start = candidate
            end = end + shift if end is not None else None
            flags.append(YEAR_INFERRED)
        elif YEAR_UNSPECIFIED not in ambiguities:
            ambiguities.append(YEAR_UNSPECIFIED)

    if (m := _THIS_WEEK_DAY.search(message)) is not None:
        monday = today - timedelta(days=today.weekday())
        day = monday + timedelta(days=_WEEKDAY_INDEX[m.group(1)])
        if day < today and THIS_WEEK_PASSED not in ambiguities:
            ambiguities.append(THIS_WEEK_PASSED)

    updated = parsed.model_copy(
        update={
            "event": event.model_copy(update={"start_local": start, "end_local": end}),
            "ambiguities": ambiguities,
        }
    )
    return DateRuleResult(updated, flags)


def has_explicit_month_day(message: str) -> bool:
    return bool(_SLASH_MD.search(message) or _MONTH_MD.search(message))


def add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year, month = d.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(d.day, monthrange(year, month)[1]))


def _with_year(value: datetime, year: int) -> datetime:
    day = min(value.day, monthrange(year, value.month)[1])  # 2/29 → 2/28
    return value.replace(year=year, day=day)
