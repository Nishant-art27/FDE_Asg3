"""Guardrails: merge model output with the deterministic findings.

The model may tighten (add flags/approvals, pick a stricter label, drop
evidence) but can never loosen a hard finding, invent evidence from a tool
that was not called, or list approvals/flags outside the taxonomy.
"""
from __future__ import annotations

import re

from src.contracts import APPROVAL_ROLES, RECOMMENDATION_LABELS, RISK_FLAGS, EvidenceItem, ProcurementDecision, RunTelemetry
from src.policy_engine import order_roles
from src.schemas import AgentAssessment, EvidenceOut, OverlapJudgement, ReviewVerdict
from src.tools import ToolSession

_EXPANSION_WORDS = ("add-on", "addon", "add on", "expansion", "expand", "additional", "extra", "more seats",
                    "training", "renewal", "renew", "upgrade seats")
_STRICTNESS = {"Approve": 0, "Route for review": 1, "Use existing tool": 1, "Request clarification": 2,
               "Manual review - evidence unavailable": 2}


def _norm_label(label: str) -> str:
    """Map free-ish model text to a canonical label."""
    l = (label or "").strip().lower()
    for canon in RECOMMENDATION_LABELS:
        if l == canon.lower():
            return canon
    if "clarif" in l or "missing" in l:
        return "Request clarification"
    if "unavailable" in l or "manual" in l:
        return "Manual review - evidence unavailable"
    if "existing" in l:
        return "Use existing tool"
    if "review" in l or "route" in l or "escalat" in l:
        return "Route for review"
    if l.startswith("approve"):
        return "Approve"
    return label


def offline_assessment(request: dict, findings: dict) -> AgentAssessment:
    """Deterministic stand-in for the model (offline mode / model unavailable)."""
    text = f"{request.get('product_name', '')} {request.get('business_justification', '')}".lower()
    overlap = bool(findings.get("overlap_candidate")) and not any(w in text for w in _EXPANSION_WORDS)
    label = findings["floor_recommendation"]
    if overlap and "Use existing tool" in findings["allowed_recommendations"] and label == "Approve":
        label = "Use existing tool"
    reasons = findings.get("reasons", {})
    bullet = "; ".join(f"{k}: {v[0]}" for k, v in reasons.items()) or "no policy rule triggered"
    summary = (f"Deterministic assessment (no model): floor recommendation '{findings['floor_recommendation']}'. "
               f"Findings - {bullet}.")
    return AgentAssessment(recommendation=label, summary=summary, evidence=[],
                           existing_tool_overlap=OverlapJudgement(applies=overlap,
                                                                  reason="catalog match without an expansion/add-on rationale"
                                                                  if overlap else "no overlap or request is an expansion of an approved product"),
                           next_step=findings["floor_next_step"],
                           prompt_injection_observed=bool(findings.get("injection_hits")))


def apply_review(assessment: AgentAssessment, verdict: ReviewVerdict, findings: dict) -> tuple[AgentAssessment, list[str]]:
    """Apply a reviewer verdict - tightening only."""
    notes: list[str] = []
    a = assessment.model_copy(deep=True)
    if verdict.drop_evidence:
        keep = [e for i, e in enumerate(a.evidence) if i not in set(verdict.drop_evidence)]
        notes.append(f"reviewer_dropped_evidence:{len(a.evidence) - len(keep)}")
        a.evidence = keep
    for fl in verdict.add_risk_flags:
        if fl in RISK_FLAGS and fl not in a.additional_risk_flags:
            a.additional_risk_flags.append(fl)
            notes.append(f"reviewer_added_flag:{fl}")
    for role in verdict.add_approvals:
        if role in APPROVAL_ROLES and role not in a.additional_approvals:
            a.additional_approvals.append(role)
            notes.append(f"reviewer_added_approval:{role}")
    if verdict.recommendation_override:
        new = _norm_label(verdict.recommendation_override)
        if new in RECOMMENDATION_LABELS and _STRICTNESS.get(new, 0) >= _STRICTNESS.get(_norm_label(a.recommendation), 0) \
                and new != a.recommendation:
            notes.append(f"reviewer_label:{a.recommendation}->{new}")
            a.recommendation = new
            if verdict.next_step_override:
                a.next_step = verdict.next_step_override
    return a, notes


def finalize(request: dict, findings: dict, assessment: AgentAssessment, session: ToolSession,
             telemetry: RunTelemetry, summary_prefix: str = "") -> ProcurementDecision:
    overrides = list(telemetry.guardrail_overrides)
    called = set(session.tool_names)

    # --- approvals: floor ∪ model additions (taxonomy only) ---
    approvals = set(findings["required_approvals"])
    for role in assessment.additional_approvals:
        if role in APPROVAL_ROLES and role not in approvals:
            approvals.add(role)
            overrides.append(f"llm_added_approval:{role}")

    # --- flags: floor ∪ model additions (taxonomy only) ---
    flags = list(findings["risk_flags"])
    for fl in assessment.additional_risk_flags:
        if fl in RISK_FLAGS and fl not in flags and fl != "existing_tool_overlap":
            flags.append(fl)
            overrides.append(f"llm_added_flag:{fl}")
    wants_overlap = assessment.existing_tool_overlap.applies or "existing_tool_overlap" in assessment.additional_risk_flags
    if wants_overlap and findings.get("overlap_candidate"):
        if "existing_tool_overlap" not in flags:
            flags.append("existing_tool_overlap")
            overrides.append("llm_added_flag:existing_tool_overlap")
    elif wants_overlap:
        overrides.append("llm_overlap_without_catalog_match_dropped")
    if assessment.prompt_injection_observed and "prompt_injection_detected" not in flags:
        flags.append("prompt_injection_detected")
        overrides.append("llm_added_flag:prompt_injection_detected")

    # --- label: must be in the allowed set; model may only tighten ---
    label = _norm_label(assessment.recommendation)
    allowed = findings["allowed_recommendations"]
    if label == "Use existing tool" and "existing_tool_overlap" not in flags:
        overrides.append(f"recommendation_override:{label}->{findings['floor_recommendation']} (no overlap flag)")
        label = findings["floor_recommendation"]
    elif label not in allowed:
        overrides.append(f"recommendation_override:{assessment.recommendation}->{findings['floor_recommendation']}")
        label = findings["floor_recommendation"]
    next_step = assessment.next_step.strip() if assessment.next_step and not any(
        o.startswith("recommendation_override") for o in overrides) else findings["floor_next_step"]
    if label == "Use existing tool" and "existing_tool_overlap" not in flags:
        flags.append("existing_tool_overlap")

    # --- evidence: deterministic items first, then grounded model items ---
    evidence: list[EvidenceItem] = []
    seen: set[str] = set()
    def _key(src: str, finding: str) -> str:
        return src + "|" + re.sub(r"\W+", " ", finding.lower()).strip()[:80]
    for e in findings["evidence"]:
        k = _key(e["source"], e["finding"])
        if k not in seen:
            seen.add(k)
            evidence.append(EvidenceItem(source=e["source"], finding=e["finding"], reference=e.get("reference")))
    dropped = 0
    for e in assessment.evidence:
        e = e if isinstance(e, EvidenceOut) else EvidenceOut.model_validate(e)
        if e.source not in called or not e.finding.strip():
            dropped += 1
            continue
        k = _key(e.source, e.finding)
        if k in seen:
            continue
        seen.add(k)
        evidence.append(EvidenceItem(source=e.source, finding=e.finding.strip(), reference=e.reference or None))
    if dropped:
        overrides.append(f"ungrounded_evidence_dropped:{dropped}")

    telemetry.guardrail_overrides = overrides
    telemetry.ungrounded_evidence_dropped = dropped
    telemetry.tool_calls = len(session.calls)
    telemetry.tool_names = session.tool_names

    return ProcurementDecision(
        request_id=request.get("request_id", "UNKNOWN"),
        recommendation=label,
        evidence=evidence[:16],
        required_approvals=order_roles(approvals),
        missing_information=list(findings["missing_information"]),
        risk_flags=flags,
        next_step=next_step or findings["floor_next_step"],
        human_review_required=True,
        telemetry=telemetry,
        summary=(summary_prefix + assessment.summary).strip() or None,
    )
