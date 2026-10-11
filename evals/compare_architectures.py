"""Run the same case set through both architectures and write a comparison.

    python evals/compare_architectures.py                # all 10 cases, both architectures
    python evals/compare_architectures.py --cases public # the 6 public cases only
    python evals/compare_architectures.py --repeats 3    # repeat each case (latency / stability)

Outputs: evals/results_comparison.csv (one row per case x architecture x repeat)
         docs/evaluation_results.md   (summary tables + per-case detail)
Starts the mock vendor-risk API automatically if it is not already running.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from evals.run_public_evals import evaluate as minimum_checks  # noqa: E402
from src.contracts import ProcurementDecision  # noqa: E402
from src.solution import handle_request  # noqa: E402

API_HEALTH = os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001").rstrip("/") + "/health"


def ensure_api() -> subprocess.Popen | None:
    try:
        if requests.get(API_HEALTH, timeout=0.5).ok:
            return None
    except requests.RequestException:
        pass
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "mock_api.app:app", "--host", "127.0.0.1", "--port", "8001",
                             "--log-level", "warning"], cwd=ROOT)
    for _ in range(40):
        time.sleep(0.25)
        try:
            if requests.get(API_HEALTH, timeout=0.5).ok:
                return proc
        except requests.RequestException:
            continue
    proc.terminate()
    raise RuntimeError("mock vendor-risk API did not start")


def score(case: dict, d: ProcurementDecision) -> dict:
    failures = minimum_checks(d, case["expectations"])
    flags = set(d.risk_flags)
    approvals = set(d.required_approvals)
    tel = d.telemetry
    called = set(tel.tool_names) if tel else set()
    grounded = bool(d.evidence) and all(e.source in called for e in d.evidence)
    policy_ok = (not failures and set(case.get("expected_flags_all", [])) <= flags
                 and not (set(case.get("forbidden_flags", [])) & flags))
    escalation_ok = d.human_review_required and set(case.get("expected_approvals_all", [])) <= approvals
    expected_labels = case.get("expected_recommendation_any") or []
    action_ok = (d.recommendation in expected_labels) if expected_labels else True
    return {
        "passed_minimum_checks": not failures,
        "correct_next_action": action_ok,
        "grounded_evidence": grounded,
        "policy_followed": policy_ok,
        "human_escalation_correct": escalation_ok,
        "all_criteria": (not failures) and action_ok and grounded and policy_ok and escalation_ok,
        "failures": " | ".join(failures),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", choices=["public", "all"], default="all")
    ap.add_argument("--architectures", nargs="+", default=["single", "staged"])
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--pause", type=float, default=float(os.getenv("EVAL_PAUSE_SECONDS", "0")),
                    help="seconds to sleep between runs (free-tier token-per-minute limits)")
    ap.add_argument("--csv", default=str(ROOT / "evals" / "results_comparison.csv"))
    ap.add_argument("--md", default=str(ROOT / "docs" / "evaluation_results.md"))
    args = ap.parse_args()

    cases = json.loads((ROOT / "evals" / "extended_cases.json").read_text(encoding="utf-8"))
    if args.cases == "public":
        cases = [c for c in cases if c["case_id"].startswith("PUB")]

    api_proc = ensure_api()
    rows: list[dict] = []
    try:
        for arch in args.architectures:
            print(f"\n=== architecture: {arch} ===", flush=True)
            for rep in range(args.repeats):
                for case in cases:
                    if args.pause and rows:
                        time.sleep(args.pause)
                    t0 = time.perf_counter()
                    try:
                        raw = handle_request(case["request_id"], architecture=arch)
                        d = raw if isinstance(raw, ProcurementDecision) else ProcurementDecision.model_validate(raw)
                        latency = (time.perf_counter() - t0) * 1000
                        s = score(case, d)
                        tel = d.telemetry
                        row = {
                            "case_id": case["case_id"], "request_id": case["request_id"], "architecture": arch, "repeat": rep + 1,
                            "correct_next_action": s["correct_next_action"], "grounded_evidence": s["grounded_evidence"],
                            "policy_followed": s["policy_followed"], "human_escalation_correct": s["human_escalation_correct"],
                            "passed_minimum_checks": s["passed_minimum_checks"], "all_criteria": s["all_criteria"],
                            "latency_ms": round(latency, 1),
                            "model_latency_ms": tel.model_latency_ms if tel and tel.model_latency_ms is not None else "",
                            "rate_limit_wait_ms": tel.rate_limit_wait_ms if tel else "",
                            "llm_calls": tel.llm_calls if tel else "",
                            "tool_calls": tel.tool_calls if tel else "", "input_tokens": tel.input_tokens if tel else "",
                            "output_tokens": tel.output_tokens if tel else "", "llm_mode": tel.llm_mode if tel else "",
                            "model": tel.model if tel else "",
                            "guardrail_overrides": "; ".join(tel.guardrail_overrides) if tel else "",
                            "recommendation": d.recommendation, "risk_flags": ", ".join(d.risk_flags),
                            "required_approvals": ", ".join(d.required_approvals),
                            "notes": s["failures"],
                        }
                        mark = "PASS" if s["all_criteria"] else "FAIL"
                        print(f"{mark} {case['case_id']} {d.recommendation:36} {latency:7.0f} ms (wait {row['rate_limit_wait_ms'] or 0} ms)  "
                              f"llm={row['llm_calls']} tools={row['tool_calls']} tok={row['input_tokens']}/{row['output_tokens']}"
                              + (f"  overrides: {row['guardrail_overrides']}" if row['guardrail_overrides'] else ""), flush=True)
                        if not s["all_criteria"]:
                            print(f"      action={s['correct_next_action']} grounded={s['grounded_evidence']} policy={s['policy_followed']} "
                                  f"escalation={s['human_escalation_correct']} {s['failures']}", flush=True)
                    except Exception as exc:  # keep going; an exception is a failed case
                        latency = (time.perf_counter() - t0) * 1000
                        print(f"ERROR {case['case_id']} {type(exc).__name__}: {exc}", flush=True)
                        row = {"case_id": case["case_id"], "request_id": case["request_id"], "architecture": arch, "repeat": rep + 1,
                               "correct_next_action": False, "grounded_evidence": False, "policy_followed": False,
                               "human_escalation_correct": False, "passed_minimum_checks": False, "all_criteria": False,
                               "latency_ms": round(latency, 1), "model_latency_ms": "", "rate_limit_wait_ms": "",
                               "llm_calls": "", "tool_calls": "", "input_tokens": "",
                               "output_tokens": "", "llm_mode": "", "model": "", "guardrail_overrides": "",
                               "recommendation": "", "risk_flags": "", "required_approvals": "",
                               "notes": f"ERROR: {type(exc).__name__}: {exc}"}
                    rows.append(row)
    finally:
        if api_proc is not None:
            api_proc.terminate()

    Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
    with open(args.csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # ---- summary -----------------------------------------------------------
    def avg(vals):
        vals = [float(v) for v in vals if v not in ("", None)]
        return statistics.mean(vals) if vals else 0.0

    md = [f"# Evaluation results", "",
          f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} - {len(cases)} cases x {args.repeats} repeat(s) per architecture.",
          f"Model: `{rows[0].get('model') or 'n/a'}` - mode: `{rows[0].get('llm_mode') or 'n/a'}`.", "",
          "| Metric | " + " | ".join(args.architectures) + " |", "|---|" + "---:|" * len(args.architectures)]
    metrics = [
        ("Cases passing ALL quality criteria", lambda r: f"{sum(1 for x in r if x['all_criteria'])}/{len(r)}"),
        ("Public-harness minimum checks passed", lambda r: f"{sum(1 for x in r if x['passed_minimum_checks'])}/{len(r)}"),
        ("Correct recommendation / next action", lambda r: f"{sum(1 for x in r if x['correct_next_action'])}/{len(r)}"),
        ("Evidence grounded in tool results", lambda r: f"{sum(1 for x in r if x['grounded_evidence'])}/{len(r)}"),
        ("Policy + deterministic rules followed", lambda r: f"{sum(1 for x in r if x['policy_followed'])}/{len(r)}"),
        ("Escalation / human review correct", lambda r: f"{sum(1 for x in r if x['human_escalation_correct'])}/{len(r)}"),
        ("Avg end-to-end latency (ms)", lambda r: f"{avg(x['latency_ms'] for x in r):.0f}"),
        ("Avg model latency excl. rate-limit waits (ms)", lambda r: f"{avg(x['model_latency_ms'] for x in r):.0f}"),
        ("Avg time waiting on provider 429s (ms)", lambda r: f"{avg(x['rate_limit_wait_ms'] for x in r):.0f}"),
        ("Avg LLM calls", lambda r: f"{avg(x['llm_calls'] for x in r):.2f}"),
        ("Avg tool calls", lambda r: f"{avg(x['tool_calls'] for x in r):.2f}"),
        ("Avg tokens (in / out)", lambda r: f"{avg(x['input_tokens'] for x in r):.0f} / {avg(x['output_tokens'] for x in r):.0f}"),
        ("Runs where guardrails had to correct the model", lambda r: f"{sum(1 for x in r if any(o.startswith(('recommendation_override', 'ungrounded', 'policy_checks_forced', 'policy_autofetched')) for o in x['guardrail_overrides'].split('; ') if o))}/{len(r)}"),
        ("Runs with ungrounded evidence dropped", lambda r: f"{sum(1 for x in r if 'ungrounded_evidence_dropped' in x['guardrail_overrides'])}/{len(r)}"),
    ]
    for name, fn in metrics:
        md.append(f"| {name} | " + " | ".join(fn([r for r in rows if r["architecture"] == a]) for a in args.architectures) + " |")
    md += ["", "## Per-case results", "",
           "| Case | Arch | Rep | Recommendation | Action | Grounded | Policy | Escalation | Latency ms | LLM | Tools | Overrides / notes |",
           "|---|---|---:|---|:-:|:-:|:-:|:-:|---:|---:|---:|---|"]
    ok = lambda b: "✅" if b else "❌"  # noqa: E731
    for r in rows:
        md.append(f"| {r['case_id']} | {r['architecture']} | {r['repeat']} | {r['recommendation']} | {ok(r['correct_next_action'])} | "
                  f"{ok(r['grounded_evidence'])} | {ok(r['policy_followed'])} | {ok(r['human_escalation_correct'])} | "
                  f"{r['latency_ms']:.0f} | {r['llm_calls']} | {r['tool_calls']} | {(r['guardrail_overrides'] + ' ' + r['notes']).strip()[:160]} |")
    Path(args.md).parent.mkdir(parents=True, exist_ok=True)
    Path(args.md).write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n" + "\n".join(md[:len(metrics) + 7]))
    print(f"\nCSV: {Path(args.csv).relative_to(ROOT)}\nMarkdown: {Path(args.md).relative_to(ROOT)}")


if __name__ == "__main__":
    main()
