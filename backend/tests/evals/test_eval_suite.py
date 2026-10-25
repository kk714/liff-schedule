"""評測集與評分邏輯的離線測試（不呼叫 NOVARK）。"""

from __future__ import annotations

from collections import Counter

import pytest

from evals.run_eval import DEFAULT_CASES, compare_baseline, load_cases
from evals.scoring import CaseScore, Outcome, score_case, summarize, validate_case


def test_case_file_is_valid_and_large_enough():
    cases = load_cases(DEFAULT_CASES)
    assert len(cases) >= 150
    cats = Counter(c["cat"] for c in cases)
    for required in ("create_basic", "create_relative", "create_missing", "update", "cancel",
                     "query", "unsupported", "injection", "draft_merge", "no_hallucination"):
        assert cats[required] >= 5, required


def make_case(**expect):
    return {"id": "x", "cat": "t", "msg": "m", "expect": expect}


def parsed(**kw):
    base = {"intent": "create", "event": None, "target": None, "query": None,
            "missing_fields": [], "ambiguities": []}
    base.update(kw)
    return Outcome(kind="parsed", parsed=base)


def test_exact_datetime_match_ignores_format():
    case = make_case(intent="create", checks={"event.start_local": "2026-11-01T14:00:00"})
    s = score_case(case, parsed(event={"start_local": "2026-11-01T14:00"}))
    assert s.intent_ok and s.fields_ok


def test_wrong_value_fails_fields_but_not_intent():
    case = make_case(intent="create", checks={"event.start_local": "2026-11-01T14:00:00"})
    s = score_case(case, parsed(event={"start_local": "2026-11-01T15:00:00"}))
    assert s.intent_ok and s.fields_ok is False
    assert "event.start_local" in s.failures[0]


def test_wrong_intent_fails_fields_too():
    case = make_case(intent="update", checks={"target.on_date": "2026-10-22"})
    s = score_case(case, parsed(intent="create", target={"on_date": "2026-10-22"}))
    assert not s.intent_ok and s.fields_ok is False


def test_null_location_expectation_rejects_hallucinated_place():
    case = make_case(intent="create", checks={"event.location": None})
    assert score_case(case, parsed(event={"location": None})).fields_ok
    assert score_case(case, parsed(event={"location": "國網中心"})).fields_ok is False


@pytest.mark.parametrize(
    "key,matcher,outcome,ok",
    [
        ("event.title", {"contains": ["國網"]}, parsed(event={"title": "國網中心拜訪"}), True),
        ("event.title", {"contains": ["國網"]}, parsed(event={"title": None}), False),
        ("missing_fields", {"includes": ["start_local"]}, parsed(missing_fields=["start_local"]), True),
        ("missing_fields", {"excludes": ["end_local"]}, parsed(missing_fields=["end_local"]), False),
        ("query.person_names", {"includes": ["王小明"]}, parsed(query={"person_names": ["王小明經理"]}), True),
        ("query.person_names", {"includes": ["Amy", "Ben"]}, parsed(query={"person_names": ["Amy"]}), False),
        ("ambiguities", {"nonempty": True}, parsed(ambiguities=[]), False),
        ("ambiguities", {"empty": True}, parsed(ambiguities=[]), True),
    ],
)
def test_matchers(key, matcher, outcome, ok):
    case = make_case(intent="create", checks={key: matcher})
    assert bool(score_case(case, outcome).fields_ok) is ok


def test_intent_list_accepts_any():
    case = make_case(intent=["unsupported", "clarify"])
    assert score_case(case, parsed(intent="clarify")).intent_ok
    assert score_case(case, parsed(intent="create")).intent_ok is False


def test_guardrail_counts_only_when_allowed():
    allowed = make_case(intent="unsupported", allow_guardrail=True)
    denied = make_case(intent="create")
    blocked = Outcome(kind="guardrail")
    assert score_case(allowed, blocked).intent_ok
    assert score_case(denied, blocked).intent_ok is False


def test_error_outcome_fails():
    case = make_case(intent="create", checks={"event.start_local": None})
    s = score_case(case, Outcome(kind="error", error_code="llm_upstream"))
    assert not s.intent_ok and s.fields_ok is False


def test_summary_thresholds():
    scores = [CaseScore(str(i), "c", True, True) for i in range(19)] + [CaseScore("19", "c", False, False)]
    outcomes = [Outcome(kind="parsed", latency_ms=1000 + i, token_usage=10) for i in range(20)]
    summary = summarize(scores, outcomes)
    assert summary["intent_accuracy"] == 0.95
    assert summary["field_accuracy"] == 0.95
    assert summary["passed"] is True
    assert summary["token_usage_total"] == 200

    scores[0] = CaseScore("0", "c", False, False)
    assert summarize(scores, outcomes)["passed"] is False


def test_compare_baseline_lists_regressions():
    baseline = {"cases": [{"id": "a", "intent_ok": True, "fields_ok": True},
                          {"id": "b", "intent_ok": False, "fields_ok": False}]}
    scores = [CaseScore("a", "c", True, False), CaseScore("b", "c", True, True)]
    assert compare_baseline(scores, baseline) == ["a"]


def test_validate_case_rejects_bad_keys():
    assert validate_case({"id": "x", "cat": "c", "msg": "m",
                          "expect": {"intent": "create", "checks": {"evnt.title": "a"}}})
    assert validate_case({"id": "x", "cat": "c", "msg": "m", "expect": {"intent": "make"}})
    assert validate_case({"id": "x", "cat": "c", "msg": "m", "now": "2026-10-21T10:00:00",
                          "expect": {"intent": "create"}})
