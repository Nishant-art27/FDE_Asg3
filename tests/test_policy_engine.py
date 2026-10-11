from __future__ import annotations

from datetime import date

import pytest

from src.data_access import get_request
from src.policy_engine import approval_tier, evaluate
from src.tools import ToolSession, policy_reference_date


def findings_for(request_id: str) -> dict:
    s = ToolSession(get_request(request_id))
    s.gather_all()
    return s.call("run_policy_checks")


@pytest.mark.parametrize("amount,roles", [
    (1000, ["Manager"]), (1000.01, ["Department Head", "Procurement"]), (10000, ["Department Head", "Procurement"]),
    (10000.01, ["Department Head", "Finance", "Procurement"]), (25000, ["Department Head", "Finance", "Procurement"]),
    (25000.01, ["Department Head", "Finance", "CFO", "Procurement"]),
])
def test_threshold_boundaries(amount, roles):
    assert approval_tier(amount)[1] == roles


def test_reference_date_comes_from_policy_not_clock():
    assert policy_reference_date() == date(2026, 9, 30)


EXPECTED = {
    "REQ-1001": ("Approve", {"Manager"}, set(), {"budget_insufficient", "security_review_required", "missing_information"}),
    "REQ-1002": ("Route for review", {"Department Head", "Finance", "Procurement", "Security", "Legal"},
                 {"existing_tool_overlap", "security_review_required", "legal_review_required"}, {"budget_insufficient"}),
    "REQ-1003": ("Route for review", {"Department Head", "Finance", "Procurement", "Security"}, {"security_review_required"}, {"existing_tool_overlap"}),
    # NeuralDesk stores data outside the region + customer PII -> cross-region data-processing issue -> Legal (s7)
    "REQ-1004": ("Route for review", {"Department Head", "Procurement", "Security", "Privacy", "Legal"},
                 {"security_review_required", "privacy_review_required", "legal_review_required",
                  "ai_tool_data_class_restriction"}, set()),
    "REQ-1005": ("Route for review", {"Department Head", "Finance", "Procurement", "Security", "Privacy", "Legal"},
                 {"budget_insufficient", "security_review_required", "privacy_review_required", "legal_review_required"}, set()),
    "REQ-1006": ("Request clarification", {"Manager"}, {"missing_information", "prompt_injection_detected"}, set()),
    "REQ-1007": ("Route for review", {"Department Head", "Finance", "Procurement", "Security"},
                 {"vendor_review_expired", "conflicting_vendor_evidence", "security_review_required"}, {"vendor_risk_unavailable"}),
    "REQ-1008": ("Approve", {"Department Head", "Procurement"}, set(), {"security_review_required"}),
    "REQ-1009": ("Manual review - evidence unavailable", {"Department Head", "Finance", "Procurement", "Security", "Legal"},
                 {"vendor_risk_unavailable", "security_review_required", "legal_review_required"}, set()),
    "REQ-1010": ("Approve", {"Manager"}, set(), {"existing_tool_overlap", "security_review_required"}),
}


@pytest.mark.parametrize("request_id", sorted(EXPECTED))
def test_policy_engine_on_dataset(request_id):
    floor, approvals, must, must_not = EXPECTED[request_id]
    f = findings_for(request_id)
    assert f["floor_recommendation"] == floor
    assert set(f["required_approvals"]) == approvals
    assert must <= set(f["risk_flags"])
    assert not (must_not & set(f["risk_flags"]))
    assert all(e["source"] for e in f["evidence"])


def test_missing_information_items_are_specific():
    f = findings_for("REQ-1006")
    joined = " ".join(f["missing_information"]).lower()
    assert "cost" in joined and "users" in joined and "data-access" in joined


def test_expiry_uses_365_day_window():
    req = get_request("REQ-1007")
    f = findings_for("REQ-1007")
    assert "vendor_review_expired" in f["risk_flags"]
    assert any("456 days" in e["finding"] for e in f["evidence"]), "days are computed from the policy reference date"


def test_engine_without_vendor_api_evidence_flags_unverified():
    req = get_request("REQ-1001")
    s = ToolSession(req)
    s.gather_all()
    s.evidence["get_vendor_risk_assessment"] = {"available": False, "error": "connection refused"}
    f = evaluate(req, s.evidence)
    assert "vendor_risk_unavailable" in f.risk_flags
    assert f.floor_recommendation == "Manual review - evidence unavailable"
    assert "Security" in f.required_approvals
