from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    source: str = Field(description="Tool/data source name")
    finding: str = Field(description="Concise factual finding")
    reference: str | None = Field(default=None, description="Optional record ID / policy section / endpoint")


class RunTelemetry(BaseModel):
    llm_calls: int | None = None
    tool_calls: int | None = None
    tool_names: list[str] = Field(default_factory=list)
    # --- extensions (optional, ignored by the public harness) ---
    architecture: str | None = None
    llm_mode: str | None = None  # "anthropic" | "offline" | "anthropic->offline_fallback"
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None
    stage_latency_ms: dict[str, float] = Field(default_factory=dict)
    model_latency_ms: float | None = None      # sum of successful model-call latencies (excludes rate-limit waits)
    rate_limit_wait_ms: float = 0.0            # time spent waiting on provider 429s
    guardrail_overrides: list[str] = Field(default_factory=list)
    ungrounded_evidence_dropped: int = 0


class ProcurementDecision(BaseModel):
    request_id: str
    recommendation: str = Field(description="Short recommendation label or sentence")
    evidence: list[EvidenceItem] = Field(default_factory=list)
    required_approvals: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    next_step: str
    human_review_required: bool = True
    telemetry: RunTelemetry | None = None
    # --- extensions ---
    summary: str | None = Field(default=None, description="Short rationale written for the human reviewer")


Architecture = Literal["single", "staged"]

# Canonical approval roles (policy section 4-7)
APPROVAL_ROLES: list[str] = [
    "Manager",
    "Department Head",
    "Procurement",
    "Finance",
    "CFO",
    "Security",
    "Privacy",
    "Legal",
]

# Risk-flag taxonomy
RISK_FLAGS: list[str] = [
    "existing_tool_overlap",
    "budget_insufficient",
    "security_review_required",
    "privacy_review_required",
    "legal_review_required",
    "vendor_review_expired",
    "conflicting_vendor_evidence",
    "vendor_risk_unavailable",
    "prompt_injection_detected",
    "missing_information",
    "ai_tool_data_class_restriction",
    "llm_unavailable",
]

# Recommendation labels (ordered from least to most restrictive is NOT implied)
RECOMMENDATION_LABELS: list[str] = [
    "Approve",
    "Route for review",
    "Use existing tool",
    "Request clarification",
    "Manual review - evidence unavailable",
]
