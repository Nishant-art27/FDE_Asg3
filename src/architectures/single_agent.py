"""Architecture A - single agent.

One model drives the whole loop: it decides which tools to call, calls the
deterministic policy engine as a tool, then writes the structured
recommendation. Guardrails then merge its output with the policy floor.
"""
from __future__ import annotations

import time

from src import prompts
from src.contracts import ProcurementDecision, RunTelemetry
from src.guardrails import finalize, offline_assessment
from src.llm import LLM, LLMUnavailable
from src.schemas import AgentAssessment
from src.tools import TOOL_SPECS, ToolSession


def run(request: dict, llm: LLM) -> ProcurementDecision:
    t0 = time.perf_counter()
    session = ToolSession(request=request)
    telemetry = RunTelemetry(architecture="single", llm_calls=0, model=llm.model, llm_mode=llm.provider,
                             input_tokens=0, output_tokens=0)
    assessment: AgentAssessment | None = None
    prefix = ""
    try:
        assessment, calls, _trace = llm.chat(
            stage="single_agent", system=prompts.system_single_agent(), user=prompts.user_single_agent(request),
            schema_model=AgentAssessment, tools=TOOL_SPECS, tool_executor=session.call, max_rounds=8)
        telemetry.llm_calls = len(calls)
        telemetry.input_tokens = sum(c.input_tokens for c in calls)
        telemetry.output_tokens = sum(c.output_tokens for c in calls)
        telemetry.stage_latency_ms["single_agent"] = round(sum(c.latency_ms for c in calls), 1)
        telemetry.model_latency_ms = round(sum(c.latency_ms for c in calls), 1)
        telemetry.rate_limit_wait_ms = round(sum(c.rate_limit_wait_ms for c in calls), 1)
    except LLMUnavailable as exc:
        telemetry.llm_mode = "offline" if not llm.available else "groq->offline_fallback"
        telemetry.guardrail_overrides.append(f"llm_unavailable:{str(exc)[:80]}")
        prefix = "[Model unavailable - deterministic assessment] "

    # The policy engine must have run (the agent may have skipped it or the model was unavailable).
    if "run_policy_checks" not in session.evidence:
        if assessment is not None:
            telemetry.guardrail_overrides.append("policy_checks_forced")
        session.call("run_policy_checks")
    if session.policy_autofetched and assessment is not None:
        telemetry.guardrail_overrides.append("policy_autofetched:" + ",".join(session.policy_autofetched))
    findings = session.evidence["run_policy_checks"]
    if assessment is None:
        assessment = offline_assessment(request, findings)
        if llm.available:
            findings["risk_flags"] = list(findings["risk_flags"]) + ["llm_unavailable"]
    telemetry.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    return finalize(request, findings, assessment, session, telemetry, summary_prefix=prefix)
