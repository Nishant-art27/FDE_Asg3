"""Exercise the Groq tool-calling loop and JSON repair path with a stub client (no network)."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from src.data_access import get_request
from src.llm import LLM, LLMUnavailable
from src.schemas import AgentAssessment
from src.solution import run_pipeline
from src.tools import TOOL_SPECS


class _ToolCall:
    def __init__(self, i, name, args):
        self.id = f"call_{i}"
        self.type = "function"
        self.function = SimpleNamespace(name=name, arguments=json.dumps(args))

    def model_dump(self):
        return {"id": self.id, "type": "function", "function": {"name": self.function.name, "arguments": self.function.arguments}}


class StubClient:
    """Scripted Groq-like client: a list of messages to return, in order."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))  # snapshot: the caller mutates `messages` in place
        content, tool_calls = self.script.pop(0)
        msg = SimpleNamespace(content=content, tool_calls=tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))


def _llm(script):
    llm = LLM(provider="offline")
    llm.provider = "groq"
    llm._client = StubClient(script)
    return llm


GOOD = json.dumps({"recommendation": "Approve", "summary": "fine", "evidence": [
    {"source": "check_department_budget", "finding": "Finance has $29,000 available", "reference": "department_budgets.csv:Finance"},
    {"source": "not_a_tool", "finding": "made up", "reference": ""}],
    "existing_tool_overlap": {"applies": False, "reason": "add-on"}, "additional_risk_flags": [], "additional_approvals": [],
    "next_step": "Obtain Manager approval", "prompt_injection_observed": False})


def test_single_agent_tool_loop_with_stub():
    req = get_request("REQ-1001")
    script = [
        ("", [_ToolCall(1, "get_requester_profile", {"employee_id": "E004"}),
              _ToolCall(2, "check_department_budget", {"department": "Finance", "annual_cost_usd": 800})]),
        ("", [_ToolCall(3, "run_policy_checks", {})]),
        (GOOD, None),
    ]
    llm = _llm(script)
    d = run_pipeline(req, "single", llm=llm)
    assert d.recommendation == "Approve"
    assert d.telemetry.llm_calls == 3
    assert d.telemetry.llm_mode == "groq"
    assert d.telemetry.input_tokens == 300
    assert "run_policy_checks" in d.telemetry.tool_names
    assert d.telemetry.ungrounded_evidence_dropped == 1
    assert any(o.startswith("policy_autofetched") for o in d.telemetry.guardrail_overrides)
    # tool results went back as role=tool messages with matching ids
    msgs = llm._client.requests[1]["messages"]
    assert [m["tool_call_id"] for m in msgs if m["role"] == "tool"] == ["call_1", "call_2"]
    assert llm._client.requests[0]["tools"][0]["function"]["name"] == TOOL_SPECS[0]["name"]


def test_staged_two_calls_and_reviewer_tightens():
    req = get_request("REQ-1008")
    analyst = json.loads(GOOD)
    analyst["recommendation"] = "Approve"
    analyst["evidence"] = [{"source": "search_software_catalog", "finding": "TaskFlow is licensed company-wide (180 seats)", "reference": "software_catalog.csv:SW003"}]
    reviewer = {"verdict": "revise", "issues": ["existing tool covers need"], "drop_evidence": [], "add_risk_flags": ["existing_tool_overlap"],
                "add_approvals": [], "recommendation_override": "Use existing tool", "next_step_override": "Ask Marketing to use TaskFlow"}
    llm = _llm([(json.dumps(analyst), None), (json.dumps(reviewer), None)])
    d = run_pipeline(req, "staged", llm=llm)
    assert d.telemetry.llm_calls == 2
    assert d.recommendation == "Use existing tool"
    assert "existing_tool_overlap" in d.risk_flags
    assert d.next_step == "Ask Marketing to use TaskFlow"
    assert llm._client.requests[0]["response_format"]["type"] == "json_schema"


def test_invalid_json_is_repaired_once_then_fails_closed():
    req = get_request("REQ-1001")
    reviewer_ok = json.dumps({"verdict": "approve", "issues": [], "drop_evidence": [], "add_risk_flags": [], "add_approvals": [],
                              "recommendation_override": "", "next_step_override": ""})
    llm = _llm([("not json at all", None), (GOOD, None), (reviewer_ok, None)])
    d = run_pipeline(req, "staged", llm=llm)
    assert d.telemetry.llm_mode == "groq" and d.telemetry.llm_calls == 3  # analyst + repair + reviewer
    llm2 = _llm([("garbage", None), ("still garbage", None), ("x", None), ("y", None)])
    d2 = run_pipeline(req, "staged", llm=llm2)
    assert d2.telemetry.llm_mode == "groq->offline_fallback"
    assert "llm_unavailable" in d2.risk_flags and d2.recommendation == "Approve"


class _BadRequest(Exception):
    """Mimics groq.BadRequestError enough for recover_failed_generation."""
    def __init__(self, body):
        super().__init__("400")
        self.body = body
        self.message = "400 tool_use_failed"


def test_final_answer_recovered_from_tool_use_failed(monkeypatch):
    import groq
    from src.llm import recover_failed_generation
    body = {"error": {"code": "tool_use_failed", "failed_generation": json.dumps({"name": "json", "arguments": json.loads(GOOD)})}}
    text, name = recover_failed_generation(_BadRequest(body), {"get_requester_profile"})
    assert name == "json" and json.loads(text)["recommendation"] == "Approve"
    # a malformed call to a REAL tool is not an answer
    body2 = {"error": {"code": "tool_use_failed", "failed_generation": '{"name": "get_requester_profile", "arguments": "{bad"}'}}
    assert recover_failed_generation(_BadRequest(body2), {"get_requester_profile"}) == (None, "get_requester_profile")


def test_single_agent_switches_to_structured_final_call_after_policy_checks():
    req = get_request("REQ-1001")
    script = [("", [_ToolCall(1, "run_policy_checks", {})]), (GOOD, None)]
    llm = _llm(script)
    d = run_pipeline(req, "single", llm=llm)
    assert d.recommendation == "Approve" and d.telemetry.llm_calls == 2
    final_req = llm._client.requests[1]
    assert "tools" not in final_req and final_req["response_format"]["type"] == "json_schema"


def test_offline_llm_raises_unavailable():
    with pytest.raises(LLMUnavailable):
        LLM(provider="offline").chat(stage="x", system="s", user="u", schema_model=AgentAssessment)
