from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.llm import ParseContext
from app.llm.schemas import ParsedEvent, ParsedSchedule
from app.rules.dates import (
    THIS_WEEK_PASSED,
    YEAR_INFERRED,
    YEAR_UNSPECIFIED,
    add_months,
    apply_date_rules,
    has_explicit_month_day,
)

TPE = ZoneInfo("Asia/Taipei")
OCT_21 = ParseContext(now=datetime(2026, 10, 21, 10, 0, tzinfo=TPE))  # 週三
DEC_28 = ParseContext(now=datetime(2026, 12, 28, 10, 0, tzinfo=TPE))
JUN_10 = ParseContext(now=datetime(2026, 6, 10, 10, 0, tzinfo=TPE))


def create(start: datetime, end: datetime | None = None, intent: str = "create") -> ParsedSchedule:
    return ParsedSchedule(intent=intent, event=ParsedEvent(title="t", start_local=start, end_local=end))


def test_a18_december_1_5_inferred_as_next_year():
    r = apply_date_rules(create(datetime(2026, 1, 5, 10)), "1/5 10:00 開會", DEC_28)
    assert r.parsed.event.start_local == datetime(2027, 1, 5, 10)
    assert r.flags == [YEAR_INFERRED]
    assert r.parsed.ambiguities == []


def test_a18_june_1_5_asks_for_year():
    r = apply_date_rules(create(datetime(2026, 1, 5, 10)), "1/5 10:00 開會", JUN_10)
    assert r.parsed.event.start_local == datetime(2026, 1, 5, 10)  # 不改值，交給追問
    assert r.flags == []
    assert r.parsed.ambiguities == [YEAR_UNSPECIFIED]


def test_end_time_shifted_with_inferred_year():
    r = apply_date_rules(
        create(datetime(2026, 3, 10, 14), datetime(2026, 3, 10, 16)), "3/10 14:00-16:00", OCT_21
    )
    assert r.parsed.event.start_local == datetime(2027, 3, 10, 14)
    assert r.parsed.event.end_local == datetime(2027, 3, 10, 16)


def test_six_month_boundary_is_inclusive():
    # 10/21 + 6 個月 = 2027-04-21
    inside = apply_date_rules(create(datetime(2026, 4, 21, 9)), "4/21 9:00", OCT_21)
    outside = apply_date_rules(create(datetime(2026, 4, 22, 9)), "4/22 9:00", OCT_21)
    assert inside.flags == [YEAR_INFERRED]
    assert outside.parsed.ambiguities == [YEAR_UNSPECIFIED]


@pytest.mark.parametrize(
    "message",
    ["2026/1/5 10:00 開會", "2026年1月5日 開會", "今年1/5 開會", "明天 10:00", "下個月5號 10點", "下週三 開會"],
)
def test_no_year_rule_without_bare_month_day(message):
    r = apply_date_rules(create(datetime(2026, 1, 5, 10)), message, OCT_21)
    assert r.flags == [] and YEAR_UNSPECIFIED not in r.parsed.ambiguities


def test_future_date_untouched():
    r = apply_date_rules(create(datetime(2026, 11, 15, 10)), "11/15 10:00 開會", OCT_21)
    assert r.parsed.event.start_local == datetime(2026, 11, 15, 10)
    assert r.flags == [] and r.parsed.ambiguities == []


def test_chinese_numeral_month_day_detected():
    assert has_explicit_month_day("十一月十日下午兩點")
    assert has_explicit_month_day("11月6日 上午10點")
    assert not has_explicit_month_day("下個月5號")


@pytest.mark.parametrize(
    "message,passed",
    [("這週一下午3點開會", True), ("本週二 10:00", True), ("這週三 15:00", False),
     ("這週五 10:00", False), ("這個禮拜日 下午", False), ("下週一 10:00", False)],
)
def test_this_week_day_passed(message, passed):
    r = apply_date_rules(create(datetime(2026, 10, 19, 15)), message, OCT_21)
    assert (THIS_WEEK_PASSED in r.parsed.ambiguities) is passed


def test_only_create_intent_is_checked():
    parsed = ParsedSchedule(intent="cancel", event=ParsedEvent(start_local=datetime(2026, 1, 5, 10)))
    r = apply_date_rules(parsed, "取消 1/5 的會議", OCT_21)
    assert r.parsed is parsed and r.flags == []


def test_missing_start_is_left_alone():
    parsed = ParsedSchedule(intent="create", event=ParsedEvent(title="x"), missing_fields=["start_local"])
    assert apply_date_rules(parsed, "1/5 開會", OCT_21).parsed is parsed


def test_feb_29_inferred_year_clamped():
    ctx = ParseContext(now=datetime(2027, 12, 1, 9, tzinfo=TPE))
    r = apply_date_rules(create(datetime(2027, 2, 28, 9)), "2/29 9:00", ctx)
    assert r.parsed.event.start_local == datetime(2028, 2, 28, 9)


def test_add_months_clamps_day():
    assert add_months(date(2026, 8, 31), 6) == date(2027, 2, 28)
    assert add_months(date(2026, 10, 21), 6) == date(2027, 4, 21)
