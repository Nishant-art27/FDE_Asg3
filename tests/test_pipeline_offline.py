"""End-to-end (offline mode): both architectures must satisfy the public harness checks."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.run_public_evals import evaluate
from src.llm import LLM
from src.solution import run_pipeline
from src.data_access import get_request

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "evals" / "extended_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("architecture", ["single", "staged"])
@pytest.mark.parametrize("case", CASES, ids=[c["case_id"] for c in CASES])
def test_offline_pipeline_meets_minimum_checks(architecture, case):
    d = run_pipeline(get_request(case["request_id"]), architecture, llm=LLM(provider="offline"))
    assert evaluate(d, case["expectations"]) == []
    assert d.recommendation in case["expected_recommendation_any"]
    assert d.telemetry.llm_calls == 0 and d.telemetry.llm_mode == "offline"
    assert all(e.source in set(d.telemetry.tool_names) for e in d.evidence)
    assert d.telemetry.tool_calls >= 3
