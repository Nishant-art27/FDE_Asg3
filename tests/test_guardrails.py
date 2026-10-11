from __future__ import annotations

from src.contracts import RunTelemetry
from src.data_access import get_request
from src.guardrails import apply_review, finalize, offline_assessment
from src.schemas import AgentAssessment, EvidenceOut, OverlapJudgement, ReviewVerdict
from src.tools import ToolSession


def _session(rid):
    s = ToolSession(get_request(rid))
    s.gather_all()
    f = s.call("run_policy_checks")
    return s, f


def test_model_cannot_loosen_recommendation():
    s, f = _session("REQ-1005")  # budget + security + privacy + legal
    a = AgentAssessment(recommendation="Approve", summary="looks fine", evidence=[], next_step="buy it",
                        existing_tool_overlap=OverlapJudgement(applies=False, reason=""))
    d = finalize(s.request, f, a, s, RunTelemetry())
    assert d.recommendation == "Route for review"
    assert any(o.startswith("recommendation_override") for o in d.telemetry.guardrail_overrides)
    assert d.next_step == f["floor_next_step"]
    assert {"Finance", "Security", "Privacy", "Legal"} <= set(d.required_approvals)


def test_ungrounded_evidence_and_unknown_roles_are_dropped():
    s, f = _session("REQ-1001")
    a = AgentAssessment(recommendation="Approve", summary="ok",
                        evidence=[EvidenceOut(source="web_search", finding="vendor is great", reference=""),
                                  EvidenceOut(source="check_department_budget", finding="Finance has $29,000 available", reference="x")],
                        additional_approvals=["CEO", "Security"], additional_risk_flags=["made_up_flag"],
                        existing_tool_overlap=OverlapJudgement(applies=False, reason=""), next_step="Obtain Manager approval")
    d = finalize(s.request, f, a, s, RunTelemetry())
    assert all(e.source != "web_search" for e in d.evidence)
    assert d.telemetry.ungrounded_evidence_dropped == 1
    assert "CEO" not in d.required_approvals and "Security" in d.required_approvals
    assert "made_up_flag" not in d.risk_flags


def test_overlap_flag_requires_catalog_match():
    s, f = _session("REQ-1005")  # GrowthForge: no catalog match
    a = AgentAssessment(recommendation="Use existing tool", summary="", evidence=[],
                        existing_tool_overlap=OverlapJudgement(applies=True, reason="hallucinated"), next_step="")
    d = finalize(s.request, f, a, s, RunTelemetry())
    assert "existing_tool_overlap" not in d.risk_flags and d.recommendation == "Route for review"


def test_reviewer_can_only_tighten():
    s, f = _session("REQ-1008")
    a = offline_assessment(s.request, f)
    v = ReviewVerdict(verdict="revise", drop_evidence=[], add_risk_flags=["existing_tool_overlap", "bogus"],
                      add_approvals=["Procurement"], recommendation_override="Approve")  # looser -> ignored
    a2, notes = apply_review(a, v, f)
    assert a2.recommendation == a.recommendation
    assert "bogus" not in a2.additional_risk_flags


def test_missing_information_is_deterministic_only():
    s, f = _session("REQ-1001")
    a = offline_assessment(s.request, f)
    d = finalize(s.request, f, a, s, RunTelemetry())
    assert d.missing_information == [] and d.human_review_required is True
