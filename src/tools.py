"""Tool layer for the procurement copilot.

Six evidence tools plus one deterministic policy tool. Every tool returns a
JSON-serialisable dict. A `ToolSession` executes tools, records telemetry
and keeps the gathered evidence so the deterministic policy engine can run
on exactly what was collected (and fill any gaps itself).

Tools 1-4 and 6 are deterministic lookups over the business data snapshot;
tool 5 calls the external (mock) vendor-risk service; tool 7 is the
deterministic policy engine (`src/policy_engine.py`).
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable
from urllib.parse import quote

import requests

from src import data_access
from src.injection import scan_fields

# ---------------------------------------------------------------------------
# Reference date (policy section header) - never the computer clock.
# ---------------------------------------------------------------------------
_DATE_RX = re.compile(r"reference date:\*\*\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)


def policy_reference_date() -> date:
    text = data_access.load_policy_text()
    m = _DATE_RX.search(text)
    return date.fromisoformat(m.group(1)) if m else date(2026, 9, 30)


SECURITY_ASSESSMENT_VALID_DAYS = 365


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _days_between(d1: date, iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return (d1 - date.fromisoformat(str(iso)[:10])).days
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 1. Requester profile (deterministic)
# ---------------------------------------------------------------------------
def get_requester_profile(employee_id: str) -> dict:
    df = data_access.load_employees()
    rows = df[df["employee_id"] == employee_id]
    if rows.empty:
        return {"found": False, "employee_id": employee_id, "error": "Unknown employee_id"}
    row = rows.iloc[0].to_dict()
    manager = df[df["employee_id"] == row.get("manager_id")]
    manager_name = manager.iloc[0]["name"] if not manager.empty else None
    return {
        "found": True,
        "employee_id": row["employee_id"],
        "name": row["name"],
        "department": row["department"],
        "manager_id": row.get("manager_id") if isinstance(row.get("manager_id"), str) else None,
        "manager_name": manager_name,
        "level": row["level"],
        "country": row["country"],
        "reference": f"employees.csv:{row['employee_id']}",
    }


# ---------------------------------------------------------------------------
# 2. Department budget (deterministic)
# ---------------------------------------------------------------------------
def check_department_budget(department: str, annual_cost_usd: float | None = None) -> dict:
    df = data_access.load_budgets()
    rows = df[df["department"].str.lower() == str(department).lower()]
    if rows.empty:
        return {"found": False, "department": department, "error": "No budget record for department"}
    row = rows.iloc[0]
    available = int(row["available_usd"])
    out = {
        "found": True,
        "department": row["department"],
        "annual_software_budget_usd": int(row["annual_software_budget_usd"]),
        "committed_usd": int(row["committed_usd"]),
        "available_usd": available,
        "requested_annual_cost_usd": annual_cost_usd,
        "within_budget": None,
        "shortfall_usd": None,
        "reference": f"department_budgets.csv:{row['department']}",
    }
    if annual_cost_usd is not None:
        cost = float(annual_cost_usd)
        out["within_budget"] = cost <= available
        out["shortfall_usd"] = max(0.0, cost - available)
    return out


# ---------------------------------------------------------------------------
# 3. Software catalog search (deterministic)
# ---------------------------------------------------------------------------
def search_software_catalog(product_name: str = "", vendor_name: str = "", category: str = "") -> dict:
    df = data_access.load_software_catalog()
    p, v, c = _norm(product_name), _norm(vendor_name), _norm(category)
    p_tokens = set(p.split())
    matches: list[dict] = []
    for _, row in df.iterrows():
        types: list[str] = []
        cp, cv, cc = _norm(row["product_name"]), _norm(row["vendor_name"]), _norm(row["category"])
        if p and (cp == p):
            types.append("same_product")
        elif p and cp and (cp in p or p in cp or (p_tokens & set(cp.split()))):
            types.append("same_product_family")
        if v and cv == v:
            types.append("same_vendor")
        if c and cc == c:
            types.append("same_category")
        if types:
            rec = {k: (v_ if not hasattr(v_, "item") else v_.item()) for k, v_ in row.to_dict().items()}
            rec["match_types"] = types
            rec["reference"] = f"software_catalog.csv:{row['software_id']}"
            matches.append(rec)
    return {"query": {"product_name": product_name, "vendor_name": vendor_name, "category": category},
            "match_count": len(matches), "matches": matches}


# ---------------------------------------------------------------------------
# 4. Vendor registry (deterministic)
# ---------------------------------------------------------------------------
def get_vendor_registry_record(vendor_name: str) -> dict:
    df = data_access.load_vendors()
    rows = df[df["vendor_name"].str.lower() == str(vendor_name).lower()]
    if rows.empty:
        return {"found": False, "vendor_name": vendor_name, "error": "Vendor not in internal registry"}
    row = rows.iloc[0].to_dict()
    ref_date = policy_reference_date()
    review_date = row.get("security_review_date") if isinstance(row.get("security_review_date"), str) else None
    days = _days_between(ref_date, review_date)
    return {
        "found": True,
        "vendor_id": row["vendor_id"],
        "vendor_name": row["vendor_name"],
        "procurement_status": row["procurement_status"],
        "security_status": row["security_status"],
        "security_review_date": review_date,
        "days_since_security_review": days,
        "security_review_current": (days is not None and days <= SECURITY_ASSESSMENT_VALID_DAYS
                                    and str(row["security_status"]).lower() == "approved"),
        "legal_terms_status": row["legal_terms_status"],
        "notes": row.get("notes") if isinstance(row.get("notes"), str) else "",
        "reference_date": ref_date.isoformat(),
        "reference": f"vendors.csv:{row['vendor_id']}",
    }


# ---------------------------------------------------------------------------
# 5. Vendor-risk service (external API; may be unavailable)
# ---------------------------------------------------------------------------
def _default_fetch(vendor_name: str, timeout: float) -> tuple[int, Any]:
    base = os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
    resp = requests.get(f"{base}/vendor-risk/{quote(vendor_name, safe='')}", timeout=timeout)
    try:
        body = resp.json()
    except ValueError:
        body = {"detail": resp.text[:200]}
    return resp.status_code, body


VendorFetcher = Callable[[str, float], tuple[int, Any]]
_vendor_fetcher: VendorFetcher = _default_fetch


def set_vendor_fetcher(fetcher: VendorFetcher | None) -> None:
    """Test hook: replace the HTTP fetcher (None restores the default)."""
    global _vendor_fetcher
    _vendor_fetcher = fetcher or _default_fetch


def get_vendor_risk_assessment(vendor_name: str, timeout_seconds: float = 3.0, retries: int = 1) -> dict:
    endpoint = f"GET /vendor-risk/{vendor_name}"
    last_error = None
    for attempt in range(retries + 1):
        try:
            status, body = _vendor_fetcher(vendor_name, timeout_seconds)
        except requests.RequestException as exc:  # connection refused / timeout
            last_error = f"{type(exc).__name__}: {exc}"
            status, body = 0, None
        if status == 200 and isinstance(body, dict):
            ref_date = policy_reference_date()
            days = _days_between(ref_date, body.get("last_review_date"))
            return {
                "available": True,
                "status_code": 200,
                "vendor_name": body.get("vendor_name", vendor_name),
                "risk_level": body.get("risk_level"),
                "security_review_status": body.get("security_review_status"),
                "last_review_date": body.get("last_review_date"),
                "days_since_review": days,
                "assessment_current": (str(body.get("security_review_status")).lower() == "approved"
                                       and days is not None and days <= SECURITY_ASSESSMENT_VALID_DAYS),
                "processes_personal_data": body.get("processes_personal_data"),
                "stores_data_outside_region": body.get("stores_data_outside_region"),
                "notes": body.get("notes", ""),
                "reference": endpoint,
            }
        if status == 404:
            return {"available": True, "status_code": 404, "vendor_name": vendor_name,
                    "security_review_status": "no_record",
                    "error": (body or {}).get("detail", "No vendor-risk record"), "reference": endpoint}
        last_error = last_error or f"HTTP {status}: {(body or {}).get('detail') if isinstance(body, dict) else body}"
        if attempt < retries:
            time.sleep(0.2)
    return {"available": False, "status_code": status, "vendor_name": vendor_name,
            "error": last_error or "Vendor-risk service unavailable", "reference": endpoint}


# ---------------------------------------------------------------------------
# 6. Purchase history (deterministic)
# ---------------------------------------------------------------------------
def get_purchase_history(vendor_name: str = "", department: str = "") -> dict:
    df = data_access.load_purchase_history()
    def rows_to_list(frame):
        out = []
        for _, r in frame.iterrows():
            rec = {k: (v.item() if hasattr(v, "item") else v) for k, v in r.to_dict().items()}
            rec["reference"] = f"purchase_history.csv:{r['purchase_id']}"
            out.append(rec)
        return out
    by_vendor = df[df["vendor_name"].str.lower() == str(vendor_name).lower()] if vendor_name else df.iloc[0:0]
    by_dept = df[df["department"].str.lower() == str(department).lower()] if department else df.iloc[0:0]
    return {"vendor_purchases": rows_to_list(by_vendor), "department_purchases": rows_to_list(by_dept)}


# ---------------------------------------------------------------------------
# Tool registry (JSON schemas are provider-neutral function schemas)
# ---------------------------------------------------------------------------
TOOL_SPECS: list[dict] = [
    {
        "name": "get_requester_profile",
        "description": "Look up the requesting employee: name, department, manager, level, country. Deterministic lookup over employees.csv.",
        "parameters": {"type": "object", "properties": {"employee_id": {"type": "string", "description": "Employee id such as E004"}},
                       "required": ["employee_id"], "additionalProperties": False},
    },
    {
        "name": "check_department_budget",
        "description": "Compare the request's annual cost with the department's available software budget (policy section 2). Deterministic.",
        "parameters": {"type": "object", "properties": {
            "department": {"type": "string"},
            "annual_cost_usd": {"type": ["number", "null"], "description": "Annual cost; null if unknown"}},
            "required": ["department", "annual_cost_usd"], "additionalProperties": False},
    },
    {
        "name": "search_software_catalog",
        "description": "Find approved catalog software with the same product, vendor or category as the request (policy section 3). Deterministic.",
        "parameters": {"type": "object", "properties": {
            "product_name": {"type": "string"}, "vendor_name": {"type": "string"}, "category": {"type": "string"}},
            "required": ["product_name", "vendor_name", "category"], "additionalProperties": False},
    },
    {
        "name": "get_vendor_registry_record",
        "description": "Internal procurement/vendor registry: procurement status, security status + review date, legal terms status. Deterministic.",
        "parameters": {"type": "object", "properties": {"vendor_name": {"type": "string"}},
                       "required": ["vendor_name"], "additionalProperties": False},
    },
    {
        "name": "get_vendor_risk_assessment",
        "description": "External vendor-risk service: risk level, security review status/date, personal-data and cross-region flags. May be unavailable - the result then has available=false and must be treated as unverified, never as favourable.",
        "parameters": {"type": "object", "properties": {"vendor_name": {"type": "string"}},
                       "required": ["vendor_name"], "additionalProperties": False},
    },
    {
        "name": "get_purchase_history",
        "description": "Previous purchases/renewals for a vendor and for a department. Deterministic.",
        "parameters": {"type": "object", "properties": {"vendor_name": {"type": "string"}, "department": {"type": "string"}},
                       "required": ["vendor_name", "department"], "additionalProperties": False},
    },
    {
        "name": "run_policy_checks",
        "description": "Deterministic policy engine. Applies required-field, budget, approval-threshold, security/privacy/legal, vendor-expiry, evidence-conflict, service-unavailable and prompt-injection rules to the evidence gathered so far in this session (it fetches any evidence you have not gathered yet). Call it once, after gathering evidence. Its hard findings are authoritative.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

EVIDENCE_TOOL_NAMES = [t["name"] for t in TOOL_SPECS if t["name"] != "run_policy_checks"]
ALL_TOOL_NAMES = [t["name"] for t in TOOL_SPECS]


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict
    result: Any
    latency_ms: float
    ok: bool = True


@dataclass
class ToolSession:
    """Executes tools for one request run and remembers what was gathered."""
    request: dict
    calls: list[ToolCallRecord] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)   # tool name -> last result
    policy_autofetched: list[str] = field(default_factory=list)

    # --- argument defaults derived from the request (used by the policy tool) ---
    def default_args(self, name: str) -> dict:
        r = self.request
        dept = (self.evidence.get("get_requester_profile") or {}).get("department") or ""
        if name == "get_requester_profile":
            return {"employee_id": r.get("requester_id", "")}
        if name == "check_department_budget":
            return {"department": dept, "annual_cost_usd": r.get("annual_cost_usd")}
        if name == "search_software_catalog":
            return {"product_name": r.get("product_name", "") or "", "vendor_name": r.get("vendor_name", "") or "",
                    "category": r.get("category", "") or ""}
        if name in ("get_vendor_registry_record", "get_vendor_risk_assessment"):
            return {"vendor_name": r.get("vendor_name", "") or ""}
        if name == "get_purchase_history":
            return {"vendor_name": r.get("vendor_name", "") or "", "department": dept}
        return {}

    def call(self, name: str, arguments: dict | None = None) -> Any:
        arguments = dict(arguments or {})
        start = time.perf_counter()
        ok = True
        try:
            if name == "run_policy_checks":
                from src.policy_engine import evaluate  # local import avoids a cycle
                self.ensure_evidence()
                result = evaluate(self.request, self.evidence).to_dict()
            elif name in _IMPL:
                result = _IMPL[name](**arguments)
            else:
                ok = False
                result = {"error": f"Unknown tool '{name}'"}
        except TypeError as exc:  # bad arguments from the model
            ok = False
            result = {"error": f"Invalid arguments for {name}: {exc}"}
        except Exception as exc:  # never let a tool crash the run
            ok = False
            result = {"error": f"{type(exc).__name__}: {exc}"}
        latency = (time.perf_counter() - start) * 1000
        self.calls.append(ToolCallRecord(name=name, arguments=arguments, result=result, latency_ms=latency, ok=ok))
        if ok:
            self.evidence[name] = result
        return result

    def ensure_evidence(self) -> None:
        """Fetch any evidence tool that has not been called yet (deterministic gap filling)."""
        for name in EVIDENCE_TOOL_NAMES:
            if name not in self.evidence:
                self.policy_autofetched.append(name)
                self.call(name, self.default_args(name))

    def gather_all(self) -> dict[str, Any]:
        """Stage-1 helper for the staged architecture: fixed-order evidence gathering."""
        for name in EVIDENCE_TOOL_NAMES:
            self.call(name, self.default_args(name))
        return self.evidence

    @property
    def tool_names(self) -> list[str]:
        return [c.name for c in self.calls]


_IMPL: dict[str, Callable[..., dict]] = {
    "get_requester_profile": get_requester_profile,
    "check_department_budget": check_department_budget,
    "search_software_catalog": search_software_catalog,
    "get_vendor_registry_record": get_vendor_registry_record,
    "get_vendor_risk_assessment": get_vendor_risk_assessment,
    "get_purchase_history": get_purchase_history,
}


def scan_request_for_injection(request: dict, evidence: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """Scan every free-text field in the request and in vendor data for instruction-like content."""
    fields = {
        "business_justification": request.get("business_justification"),
        "product_name": request.get("product_name"),
        "category": request.get("category"),
        "requested_integrations": request.get("requested_integrations"),
        "data_access_level": request.get("data_access_level"),
    }
    if evidence:
        reg = evidence.get("get_vendor_registry_record") or {}
        risk = evidence.get("get_vendor_risk_assessment") or {}
        fields["vendor_registry_notes"] = reg.get("notes")
        fields["vendor_risk_notes"] = risk.get("notes")
        for i, m in enumerate((evidence.get("search_software_catalog") or {}).get("matches", [])):
            fields[f"catalog_notes[{i}]"] = m.get("notes")
    return scan_fields(fields)
