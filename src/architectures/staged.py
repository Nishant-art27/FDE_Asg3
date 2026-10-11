"""Architecture B - staged / 2-agent variant.

Stage 1 (code, no model): fixed-order evidence gathering with all tools + policy engine.
Stage 2 (Analyst agent): interprets evidence, judges overlap, writes the package.
Stage 3 (Reviewer agent): checks grounding + policy compliance; can only tighten.
Guardrails then merge with the policy floor exactly as in Architecture A.
"""
from __future__ import annotations

import time

from src import prompts
from src.contracts import ProcurementDecision, RunTelemetry
from src.guardrails import apply_review, finalize, offline_assessment
from src.llm import LLM, LLMUnavailable
from src.schemas import AgentAssessment, ReviewVerdict
from src.tools import ToolSession


def run(request: dict, llm: LLM, with_reviewer: bool = True) -> ProcurementDecision:
    t0 = time.perf_counter()
    session = ToolSession(request=request)
    telemetry = RunTelemetry(architecture="staged", llm_calls=0, model=llm.model, llm_mode=llm.provider,
                             input_tokens=0, output_tokens=0)

    # ---- Stage 1: deterministic gathering + policy engine ----
    s1 = time.perf_counter()
    session.gather_all()
    findings = session.call("run_policy_checks")
    telemetry.stage_latency_ms["stage1_gather"] = round((time.perf_counter() - s1) * 1000, 1)
    evidence = {k: v for k, v in session.evidence.items() if k != "run_policy_checks"}

    prefix = ""
    assessment: AgentAssessment | None = None
    try:
        # ---- Stage 2: Analyst ----
        s2 = time.perf_counter()
        assessment, calls, _ = llm.chat(stage="analyst", system=prompts.system_analyst(),
                                        user=prompts.user_analyst(request, evidence, findings),
                                        schema_model=AgentAssessment)
        telemetry.stage_latency_ms["stage2_analyst"] = round(sum(c.latency_ms for c in calls), 1)
        all_calls = list(calls)
        # ---- Stage 3: Reviewer ----
        if with_reviewer:
            s3 = time.perf_counter()
            verdict, calls3, _ = llm.chat(stage="reviewer", system=prompts.system_reviewer(),
                                          user=prompts.user_reviewer(request, evidence, findings, assessment.model_dump()),
                                          schema_model=ReviewVerdict)
            telemetry.stage_latency_ms["stage3_reviewer"] = round(sum(c.latency_ms for c in calls3), 1)
            all_calls += calls3
            assessment, notes = apply_review(assessment, verdict, findings)
            telemetry.guardrail_overrides.extend(notes)
            if verdict.verdict == "revise":
                telemetry.guardrail_overrides.append("reviewer_verdict:revise")
        telemetry.llm_calls = len(all_calls)
        telemetry.model_latency_ms = round(sum(c.latency_ms for c in all_calls), 1)
        telemetry.rate_limit_wait_ms = round(sum(c.rate_limit_wait_ms for c in all_calls), 1)
        telemetry.input_tokens = sum(c.input_tokens for c in all_calls)
        telemetry.output_tokens = sum(c.output_tokens for c in all_calls)
    except LLMUnavailable as exc:
        telemetry.llm_mode = "offline" if not llm.available else "groq->offline_fallback"
        telemetry.guardrail_overrides.append(f"llm_unavailable:{str(exc)[:80]}")
        prefix = "[Model unavailable - deterministic assessment] "
        if assessment is None:
            assessment = offline_assessment(request, findings)
        if llm.available:
            findings["risk_flags"] = list(findings["risk_flags"]) + ["llm_unavailable"]

    telemetry.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    return finalize(request, findings, assessment, session, telemetry, summary_prefix=prefix)
