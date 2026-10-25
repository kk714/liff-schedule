"""對真實 NOVARK 執行 NL 解析評測（設計文件 §14.1）。

    cd backend
    NOVARK_SERVICE_TOKEN=sat_... python -m evals.run_eval
    python -m evals.run_eval --category update --limit 5
    python -m evals.run_eval --baseline evals/baseline.json
    python -m evals.run_eval --save-baseline

未達門檻（意圖 ≥95%、欄位 ≥90%）時以 exit code 1 結束；與基準相比有退步的案例會列出。
換 NOVARK 模型／服務或修改 app/llm/prompts.py 時必須重跑。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.llm import NovarkAdapter, NovarkSettings, ParseContext
from app.llm.errors import LLMError, LLMGuardrailBlocked
from app.rules.dates import apply_date_rules

from .scoring import DEFAULT_NOW, CaseScore, Outcome, score_case, summarize, validate_case

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_CASES = EVAL_DIR / "cases.jsonl"
DEFAULT_BASELINE = EVAL_DIR / "baseline.json"
RESULTS_DIR = EVAL_DIR / "results"


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        case = json.loads(line)
        if problems := validate_case(case):
            raise ValueError(f"{path}:{lineno} {case.get('id')}: {problems}")
        cases.append(case)
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    return cases


async def run_case(adapter: NovarkAdapter, case: dict[str, Any], sem: asyncio.Semaphore) -> Outcome:
    ctx = ParseContext(now=datetime.fromisoformat(case.get("now", DEFAULT_NOW)))
    async with sem:
        try:
            result = await adapter.parse_schedule(
                case["msg"], ctx, request_id=str(uuid.uuid4()), pending_draft=case.get("draft")
            )
        except LLMGuardrailBlocked:
            return Outcome(kind="guardrail", error_code="llm_guardrail_blocked")
        except LLMError as exc:
            cause = exc.__cause__
            detail = f"http={exc.http_status} {exc}"
            if cause is not None:
                detail += f" <- {type(cause).__name__}: {cause}"
            return Outcome(kind="error", error_code=exc.code, error_detail=detail[:300])
    # 評的是「模型＋後端日期規則」整條解析管線（年份與「這週X」由後端判斷）
    ruled = apply_date_rules(result.parsed, case["msg"], ctx)
    return Outcome(
        kind="parsed",
        parsed={**ruled.parsed.model_dump(mode="json"), "date_flags": ruled.flags},
        latency_ms=sum(c.duration_ms or 0 for c in result.calls),
        token_usage=sum(c.token_usage or 0 for c in result.calls),
    )


def compare_baseline(scores: list[CaseScore], baseline: dict[str, Any]) -> list[str]:
    before = {c["id"]: c for c in baseline.get("cases", [])}
    regressions = []
    for s in scores:
        old = before.get(s.id)
        if old is None:
            continue
        old_ok = old["intent_ok"] and old["fields_ok"] is not False
        new_ok = s.intent_ok and s.fields_ok is not False
        if old_ok and not new_ok:
            regressions.append(s.id)
    return regressions


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--category", help="只跑指定分類")
    parser.add_argument("--limit", type=int)
    # NOVARK 實測同時 4 個請求即回 429（retry-after: 1），預設保守值 2
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--baseline", type=Path, help="與基準結果比較")
    parser.add_argument("--save-baseline", action="store_true", help=f"把本次結果存為 {DEFAULT_BASELINE.name}")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    if args.category:
        cases = [c for c in cases if c["cat"] == args.category]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2

    settings = NovarkSettings.from_env()
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient() as client:
        adapter = NovarkAdapter(settings, client)
        outcomes = await asyncio.gather(*(run_case(adapter, c, sem) for c in cases))

    scores = [score_case(c, o) for c, o in zip(cases, outcomes)]
    summary = summarize(scores, outcomes)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "settings": {"agent": settings.agent, "input_guard": settings.input_guard,
                     "temperature": settings.temperature},
        "summary": summary,
        "cases": [
            {**asdict(s), "msg": c["msg"], "outcome": o.kind, "error_code": o.error_code,
             "error_detail": o.error_detail, "parsed": o.parsed}
            for c, s, o in zip(cases, scores, outcomes)
        ],
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"eval-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.save_baseline:
        DEFAULT_BASELINE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"cases={summary['cases']}  intent={summary['intent_accuracy']:.1%}  "
          f"fields={summary['field_accuracy']:.1%}  errors={summary['errors']}  "
          f"guardrail={summary['guardrail_blocks']}  p50={summary['latency_ms_p50']}ms  "
          f"p95={summary['latency_ms_p95']}ms  tokens={summary['token_usage_total']}")
    for cat, c in sorted(summary["by_category"].items()):
        fields = f"{c['fields_ok']}/{c['fields_n']}" if c["fields_n"] else "-"
        print(f"  {cat:<18} intent {c['intent_ok']}/{c['n']}  fields {fields}")
    failed = [s for s in scores if not s.intent_ok or s.fields_ok is False]
    for s in failed:
        print(f"  FAIL {s.id}: {'; '.join(s.failures)}")

    if args.baseline:
        regressions = compare_baseline(scores, json.loads(args.baseline.read_text(encoding="utf-8")))
        print(f"regressions vs baseline: {regressions or 'none'}")
    print(f"report: {out}")
    print("PASS" if summary["passed"] else "BELOW THRESHOLD")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
