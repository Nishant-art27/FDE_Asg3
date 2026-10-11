"""Deterministic policy engine (the CODE layer of AI / CODE / HUMAN).

Input : the request + the evidence gathered by the tools in `src/tools.py`.
Output: `PolicyFindings` - hard, reproducible findings that the LLM may
        tighten but never loosen (enforced by `src/guardrails.py`).

Every rule cites the section of data/procurement_policy.md it implements.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from src.contracts import APPROVAL_ROLES
from src.tools import SECURITY_ASSESSMENT_VALID_DAYS, policy_reference_date, scan_request_for_injection

POLICY = "procurement_policy.md"

# Data-access levels that trigger Security review (section 5)
_SECURITY_DATA_TOKENS = ("source_code", "source code", "production", "cloud", "confidential", "employee_pii",
                         "customer_pii", "pii", "credential", "secret")
_PII_TOKENS = ("employee_pii", "customer_pii", "pii", "personal")
_SECURITY_INTEGRATION_TOKENS = ("production", "cloud", "git", "repositor", "source", "aws", "gcp", "azure", "kubernetes")
_NONE_LEVELS = {"", "none", "no data", "n/a"}
_UNKNOWN_LEVELS = {"unknown", "unspecified", "tbd", "not specified"}


def approval_tier(cost: float) -> tuple[str, list[str]]:
    """Policy section 4 thresholds (minimum business approvals)."""
    if cost <= 1000:
        return "Up to $1,000", ["Manager"]
    if cost <= 10000:
        return "$1,000.01 - $10,000", ["Department Head", "Procurement"]
    if cost <= 25000:
        return "$10,000.01 - $25,000", ["Department Head", "Finance", "Procurement"]
    return "Above $25,000", ["Department Head", "Finance", "CFO", "Procurement"]


def order_roles(roles: set[str] | list[str]) -> list[str]:
    s = set(roles)
    return [r for r in APPROVAL_ROLES if r in s]


@dataclass
class PolicyFindings:
    reference_date: str
    missing_information: list[str] = field(default_factory=list)
    required_approvals: list[str] = field(default_factory=list)
    approval_tier: str | None = None
    risk_flags: list[str] = field(default_factory=list)          # hard flags (authoritative)
    overlap_candidate: bool = False                               # LLM judges (section 3)
    overlap_hard: bool = False                                    # code decided (different vendor, same category / exact dup)
    reasons: dict[str, list[str]] = field(default_factory=dict)   # flag -> reasons
    evidence: list[dict] = field(default_factory=list)            # grounded evidence items
    floor_recommendation: str = "Route for review"
    allowed_recommendations: list[str] = field(default_factory=list)
    floor_next_step: str = ""
    injection_hits: dict[str, list[str]] = field(default_factory=dict)
    unverified: list[str] = field(default_factory=list)           # things we could not verify

    def to_dict(self) -> dict:
        return asdict(self)

    def add_flag(self, flag: str, reason: str) -> None:
        if flag not in self.risk_flags:
            self.risk_flags.append(flag)
        self.reasons.setdefault(flag, [])
        if reason not in self.reasons[flag]:
            self.reasons[flag].append(reason)

    def add_evidence(self, source: str, finding: str, reference: str | None) -> None:
        self.evidence.append({"source": source, "finding": finding, "reference": reference})


def _money(v: Any) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def evaluate(request: dict, evidence: dict[str, Any]) -> PolicyFindings:  # noqa: C901 - rule table
    ref = policy_reference_date()
    f = PolicyFindings(reference_date=ref.isoformat())
    approvals: set[str] = set()

    req_profile = evidence.get("get_requester_profile") or {}
    budget = evidence.get("check_department_budget") or {}
    catalog = evidence.get("search_software_catalog") or {}
    registry = evidence.get("get_vendor_registry_record") or {}
    risk = evidence.get("get_vendor_risk_assessment") or {}
    history = evidence.get("get_purchase_history") or {}

    cost = request.get("annual_cost_usd")
    users = request.get("user_count")
    level_raw = str(request.get("data_access_level") or "").strip()
    level = level_raw.lower().replace("-", "_")
    integrations = request.get("requested_integrations")
    integ_text = " ".join(str(i) for i in (integrations or [])).lower()
    category = str(request.get("category") or "")
    is_ai_tool = "ai" in {t for t in category.lower().replace("/", " ").split()}

    # ---- Section 1: required information ---------------------------------
    if not req_profile.get("found"):
        f.missing_information.append("Requester could not be matched to an employee record (requester and department)")
    if not str(request.get("product_name") or "").strip() or not str(request.get("vendor_name") or "").strip():
        f.missing_information.append("Product/vendor is missing")
    if cost is None or (isinstance(cost, (int, float)) and cost <= 0):
        f.missing_information.append("Annual cost (or a reasonable annual estimate) is missing")
    if users is None or (isinstance(users, (int, float)) and users <= 0):
        f.missing_information.append("Number of users/licenses is missing")
    if len(str(request.get("business_justification") or "").strip()) < 15:
        f.missing_information.append("Business purpose is missing")
    if level in _UNKNOWN_LEVELS or not level:
        f.missing_information.append("Intended data-access level is unknown")
    if integrations is None:
        f.missing_information.append("Required integrations are not specified")
    if f.missing_information:
        f.add_flag("missing_information", f"{len(f.missing_information)} required field(s) missing ({POLICY} s1)")

    # ---- Requester evidence -----------------------------------------------
    if req_profile.get("found"):
        f.add_evidence("get_requester_profile",
                       f"Requester {req_profile['name']} ({req_profile['employee_id']}), {req_profile['department']}, "
                       f"reports to {req_profile.get('manager_name') or 'n/a'} ({req_profile.get('manager_id') or 'n/a'})",
                       req_profile.get("reference"))

    # ---- Section 2: budget ------------------------------------------------
    if budget.get("found"):
        if cost is not None and budget.get("within_budget") is not None:
            if budget["within_budget"]:
                f.add_evidence("check_department_budget",
                               f"{budget['department']} available software budget {_money(budget['available_usd'])} "
                               f"covers the requested {_money(cost)} (within budget; s2)", budget.get("reference"))
            else:
                f.add_flag("budget_insufficient",
                           f"Requested {_money(cost)} exceeds {budget['department']} available budget "
                           f"{_money(budget['available_usd'])} by {_money(budget['shortfall_usd'])} ({POLICY} s2)")
                approvals.add("Finance")
                f.add_evidence("check_department_budget",
                               f"Requested {_money(cost)} exceeds {budget['department']} available budget "
                               f"{_money(budget['available_usd'])} (shortfall {_money(budget['shortfall_usd'])}); "
                               f"Finance budget-exception review required (s2)", budget.get("reference"))
        else:
            f.add_evidence("check_department_budget",
                           f"{budget['department']} available software budget is {_money(budget['available_usd'])}; "
                           f"budget check cannot be completed until the annual cost is known", budget.get("reference"))
            f.unverified.append("budget check (cost unknown)")
    else:
        f.unverified.append("department budget record")

    # ---- Section 4: thresholds ----------------------------------------------
    if cost is not None and isinstance(cost, (int, float)) and cost > 0:
        tier, roles = approval_tier(float(cost))
        f.approval_tier = tier
        approvals.update(roles)
        f.add_evidence("run_policy_checks",
                       f"Annual amount {_money(cost)} is in tier '{tier}' -> minimum approvals: {', '.join(roles)} (s4)",
                       f"{POLICY} s4")
    else:
        approvals.add("Manager")
        f.add_evidence("run_policy_checks",
                       "Approval tier cannot be determined because the annual cost is missing; Manager is the minimum (s4)",
                       f"{POLICY} s4")

    # ---- Section 3: overlap ---------------------------------------------------
    matches = catalog.get("matches", []) if isinstance(catalog, dict) else []
    req_vendor = str(request.get("vendor_name") or "").lower()
    for m in matches:
        types = m.get("match_types", [])
        same_vendor = str(m.get("vendor_name", "")).lower() == req_vendor
        f.add_evidence("search_software_catalog",
                       f"Catalog has {m['product_name']} ({m['vendor_name']}, {m['category']}, status '{m['status']}', "
                       f"{m['licensed_seats']} seats, scope {m['scope']}) - match: {', '.join(types)}", m.get("reference"))
        if "same_product" in types or (("same_category" in types) and not same_vendor):
            f.overlap_hard = True
        if types:
            f.overlap_candidate = True
        if is_ai_tool and "limited" in str(m.get("status", "")).lower() and (
                any(t in level for t in _SECURITY_DATA_TOKENS) or level in _UNKNOWN_LEVELS):
            f.add_flag("ai_tool_data_class_restriction",
                       f"{m['product_name']} is '{m['status']}' - prior approval does not cover "
                       f"'{level_raw or 'unknown'}' data ({POLICY} s8)")
    if f.overlap_hard:
        f.add_flag("existing_tool_overlap",
                   "An approved catalog product in the same category / the same product already exists; "
                   f"the request must show a credible gap ({POLICY} s3)")
    if not matches and isinstance(catalog, dict) and catalog.get("match_count") == 0:
        f.add_evidence("search_software_catalog", "No existing catalog product matches the product, vendor or category (s3)",
                       "software_catalog.csv")

    # ---- Section 5: security ----------------------------------------------------
    sec_reasons: list[str] = []
    if any(t in level for t in _SECURITY_DATA_TOKENS):
        sec_reasons.append(f"data-access level '{level_raw}' is security-sensitive")
    if any(t in integ_text for t in _SECURITY_INTEGRATION_TOKENS):
        sec_reasons.append(f"integration(s) '{', '.join(integrations or [])}' touch production/cloud/source systems")

    reg_found = bool(registry.get("found"))
    reg_sec = str(registry.get("security_status") or "").lower() if reg_found else ""
    reg_days = registry.get("days_since_security_review") if reg_found else None
    reg_current = bool(registry.get("security_review_current")) if reg_found else False
    vendor_is_new = (not reg_found) or str(registry.get("procurement_status", "")).lower() != "approved"

    if reg_found:
        f.add_evidence("get_vendor_registry_record",
                       f"Registry {registry['vendor_id']}: procurement '{registry['procurement_status']}', security "
                       f"'{registry['security_status']}' (reviewed {registry.get('security_review_date') or 'never'}"
                       f"{f', {reg_days} days before {ref.isoformat()}' if reg_days is not None else ''}), "
                       f"legal terms '{registry['legal_terms_status']}'", registry.get("reference"))
    else:
        f.add_evidence("get_vendor_registry_record", "Vendor is not in the internal registry (treated as new vendor)",
                       "vendors.csv")

    api_available = bool(risk.get("available"))
    api_status = str(risk.get("security_review_status") or "").lower() if api_available else ""
    api_days = risk.get("days_since_review") if api_available else None
    api_current = bool(risk.get("assessment_current")) if api_available else False

    if api_available and risk.get("status_code") == 200:
        f.add_evidence("get_vendor_risk_assessment",
                       f"Vendor-risk service: risk '{risk.get('risk_level')}', security review '{api_status}' "
                       f"(last {risk.get('last_review_date') or 'never'}"
                       f"{f', {api_days} days before {ref.isoformat()}' if api_days is not None else ''}), "
                       f"processes personal data={risk.get('processes_personal_data')}, "
                       f"stores data outside region={risk.get('stores_data_outside_region')}", risk.get("reference"))
    elif api_available and risk.get("status_code") == 404:
        f.add_evidence("get_vendor_risk_assessment", "Vendor-risk service has no record for this vendor (assessment missing)",
                       risk.get("reference"))
    else:
        f.add_flag("vendor_risk_unavailable",
                   f"Vendor-risk service could not be reached ({risk.get('error') or 'unavailable'}); "
                   f"security status is unverified ({POLICY} s10)")
        f.unverified.append("vendor security assessment (service unavailable)")
        f.add_evidence("get_vendor_risk_assessment",
                       f"Vendor-risk service unavailable: {risk.get('error') or 'no response'} - status NOT verified (s10)",
                       risk.get("reference"))

    # Current assessment? (365 days from review date, s5)
    expired_reasons: list[str] = []
    if reg_found and reg_days is not None and reg_days > SECURITY_ASSESSMENT_VALID_DAYS:
        expired_reasons.append(f"registry review is {reg_days} days old (> {SECURITY_ASSESSMENT_VALID_DAYS})")
    if api_available and api_days is not None and api_days > SECURITY_ASSESSMENT_VALID_DAYS:
        expired_reasons.append(f"service review is {api_days} days old (> {SECURITY_ASSESSMENT_VALID_DAYS})")
    if api_available and api_status == "expired":
        expired_reasons.append("service reports status 'expired'")
    if expired_reasons:
        f.add_flag("vendor_review_expired", "; ".join(expired_reasons) + f" ({POLICY} s5)")
        sec_reasons.append("vendor security assessment is expired")

    assessment_missing = False
    if reg_found and reg_sec not in ("approved",):
        assessment_missing = True
        sec_reasons.append(f"registry security status is '{registry.get('security_status')}'")
    if not reg_found:
        assessment_missing = True
        sec_reasons.append("vendor is not in the registry")
    if api_available and api_status not in ("approved",) and api_status != "expired":
        assessment_missing = True
        sec_reasons.append(f"vendor-risk service status is '{api_status or 'unknown'}'")
    if not api_available:
        sec_reasons.append("vendor security assessment could not be verified (service unavailable)")

    # Conflict between registry and service (s5)
    if reg_found and api_available and risk.get("status_code") == 200:
        reg_ok = reg_sec == "approved"
        api_ok = api_status == "approved"
        conflict: list[str] = []
        if reg_ok != api_ok:
            conflict.append(f"registry says '{registry.get('security_status')}' but service says '{api_status}'")
        if registry.get("security_review_date") and risk.get("last_review_date") and \
                str(registry.get("security_review_date")) != str(risk.get("last_review_date")):
            conflict.append(f"review dates differ ({registry.get('security_review_date')} vs {risk.get('last_review_date')})")
        if conflict:
            f.add_flag("conflicting_vendor_evidence", "; ".join(conflict) + f" - do not silently choose one ({POLICY} s5)")
            sec_reasons.append("internal registry and vendor-risk service disagree")

    if sec_reasons:
        f.add_flag("security_review_required", "; ".join(dict.fromkeys(sec_reasons)) + f" ({POLICY} s5)")
        approvals.add("Security")

    # ---- Section 6: privacy -------------------------------------------------------
    priv_reasons: list[str] = []
    if any(t in level for t in _PII_TOKENS):
        priv_reasons.append(f"tool will process '{level_raw}'")
    outside_region = bool(risk.get("stores_data_outside_region")) if api_available else False
    if outside_region and level not in _NONE_LEVELS and level not in _UNKNOWN_LEVELS:
        priv_reasons.append("vendor stores data outside the operating region")
    if priv_reasons:
        f.add_flag("privacy_review_required", "; ".join(priv_reasons) + f" ({POLICY} s6)")
        approvals.add("Privacy")
    elif outside_region and level in _UNKNOWN_LEVELS:
        f.add_evidence("run_policy_checks",
                       "Vendor stores data outside the operating region; Privacy review will be required once the data "
                       "class is known (s6)", f"{POLICY} s6")

    # ---- Section 7: legal -------------------------------------------------------
    legal_reasons: list[str] = []
    if vendor_is_new and cost is not None and isinstance(cost, (int, float)) and cost >= 10000:
        legal_reasons.append(f"new vendor with annual spend {_money(cost)} (>= $10,000)")
    legal_status = str(registry.get("legal_terms_status") or "").lower() if reg_found else "unknown"
    if legal_status not in ("approved", "standard"):
        legal_reasons.append(f"legal terms status is '{registry.get('legal_terms_status') if reg_found else 'unknown'}'")
    if outside_region and any(t in level for t in _PII_TOKENS):
        legal_reasons.append("cross-region processing of personal data (material data-processing issue)")
    if legal_reasons:
        f.add_flag("legal_review_required", "; ".join(legal_reasons) + f" ({POLICY} s7)")
        approvals.add("Legal")

    # ---- Section 8: AI tools ------------------------------------------------------
    if is_ai_tool:
        f.add_evidence("run_policy_checks",
                       "AI tool: normal budget/security/privacy/legal rules apply; a prior purchase does not authorise "
                       "new data classes or use cases (s8)", f"{POLICY} s8")

    # ---- Section 9: untrusted content / prompt injection ----------------------------
    hits = scan_request_for_injection(request, evidence)
    if hits:
        f.injection_hits = hits
        where = ", ".join(f"{k} [{'/'.join(v)}]" for k, v in hits.items())
        f.add_flag("prompt_injection_detected",
                   f"Instruction-like content in business data: {where}. Ignored; policy still applies ({POLICY} s9)")
        f.add_evidence("run_policy_checks",
                       f"Business data contains instruction-like text ({where}); it was treated as data, not instructions (s9)",
                       f"{POLICY} s9")

    # ---- Purchase history evidence -----------------------------------------------------
    for p in (history.get("vendor_purchases") or [])[:3]:
        f.add_evidence("get_purchase_history",
                       f"Prior purchase {p['purchase_id']}: {p['department']} bought {p['product_name']} from {p['vendor_name']} "
                       f"on {p['purchase_date']} for {_money(p['annual_amount_usd'])}/yr ({p['status']}; {p.get('notes', '')})",
                       p.get("reference"))
    if not history.get("vendor_purchases") and history:
        f.add_evidence("get_purchase_history", "No prior purchases from this vendor (new commercial relationship)",
                       "purchase_history.csv")

    # ---- Floor recommendation + allowed labels ------------------------------------------
    f.required_approvals = order_roles(approvals)
    reviews = {"security_review_required", "privacy_review_required", "legal_review_required", "budget_insufficient",
               "vendor_review_expired", "conflicting_vendor_evidence", "ai_tool_data_class_restriction"}
    requester = f"{req_profile.get('name', 'the requester')} ({request.get('requester_id', '?')})"
    review_roles = [r for r in f.required_approvals if r in ("Security", "Privacy", "Legal", "Finance")]

    if f.missing_information:
        f.floor_recommendation = "Request clarification"
        f.allowed_recommendations = ["Request clarification"]
        f.floor_next_step = (f"Return the request to {requester} for: " + "; ".join(f.missing_information) +
                             ". Do not route for approval until the request is complete.")
    elif "vendor_risk_unavailable" in f.risk_flags:
        f.floor_recommendation = "Manual review - evidence unavailable"
        f.allowed_recommendations = ["Manual review - evidence unavailable"]
        f.floor_next_step = ("Vendor security status could not be verified. Send the evidence package to Security and "
                             "Procurement for manual review; retry the vendor-risk service before any approval.")
    elif f.risk_flags & reviews if isinstance(f.risk_flags, set) else set(f.risk_flags) & reviews:
        f.floor_recommendation = "Route for review"
        f.allowed_recommendations = ["Route for review", "Use existing tool", "Request clarification"]
        f.floor_next_step = (f"Send the evidence package to {', '.join(review_roles) or 'the reviewers'} and obtain "
                             f"{', '.join(f.required_approvals)} approvals before purchase.")
    else:
        f.floor_recommendation = "Approve"
        f.allowed_recommendations = ["Approve", "Use existing tool", "Route for review", "Request clarification"]
        f.floor_next_step = (f"Obtain {', '.join(f.required_approvals)} approval; Procurement then executes the purchase. "
                             "The copilot does not approve or purchase.")
    if f.overlap_hard and f.floor_recommendation == "Approve":
        f.allowed_recommendations = ["Use existing tool", "Route for review", "Approve", "Request clarification"]
    return f
