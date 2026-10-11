"""Structured-output schemas exchanged with the model (validated with pydantic)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class EvidenceOut(BaseModel):
    source: str = Field(description="Name of the tool whose output this restates")
    finding: str = Field(description="One factual sentence taken from that tool's output")
    reference: str = Field(default="", description="Record id / endpoint / policy section")


class OverlapJudgement(BaseModel):
    applies: bool = Field(description="True if an existing catalog product could reasonably satisfy the need or the request duplicates it")
    reason: str = ""


class AgentAssessment(BaseModel):
    recommendation: str = Field(description="One of the allowed recommendation labels")
    summary: str = Field(description="2-4 sentences for the human reviewer: what was found and why this recommendation")
    evidence: list[EvidenceOut] = Field(default_factory=list)
    existing_tool_overlap: OverlapJudgement = Field(default_factory=lambda: OverlapJudgement(applies=False, reason=""))
    additional_risk_flags: list[str] = Field(default_factory=list)
    additional_approvals: list[str] = Field(default_factory=list)
    next_step: str = ""
    prompt_injection_observed: bool = False


class ReviewVerdict(BaseModel):
    verdict: Literal["approve", "revise"] = "approve"
    issues: list[str] = Field(default_factory=list)
    drop_evidence: list[int] = Field(default_factory=list, description="0-based indices of analyst evidence items that are not supported by tool output")
    add_risk_flags: list[str] = Field(default_factory=list)
    add_approvals: list[str] = Field(default_factory=list)
    recommendation_override: str = Field(default="", description="A stricter label, or empty string for none")
    next_step_override: str = ""


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema cleaned for provider structured-output endpoints."""
    schema = model.model_json_schema()

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            node.pop("title", None)
            if node.get("type") == "object":
                node.setdefault("additionalProperties", False)
                props = node.get("properties", {})
                node["required"] = list(props.keys())
                for v in props.values():
                    v.pop("default", None)
            for v in node.values():
                clean(v)
        elif isinstance(node, list):
            for v in node:
                clean(v)
        return node

    return clean(schema)
