"""Assessment adapter: handle_request(request_id, architecture) -> ProcurementDecision.

`run_pipeline` is the real entry point (used by the UI, the evals and the
adapter) so ad-hoc requests that are not in requests.json can be analysed too.
"""
from __future__ import annotations

from functools import lru_cache

from src.architectures import single_agent, staged
from src.contracts import Architecture, ProcurementDecision
from src.data_access import get_request
from src.llm import LLM


@lru_cache(maxsize=1)
def default_llm() -> LLM:
    return LLM()


def run_pipeline(request: dict, architecture: Architecture = "single", llm: LLM | None = None) -> ProcurementDecision:
    llm = llm or default_llm()
    if architecture == "single":
        return single_agent.run(request, llm)
    if architecture == "staged":
        return staged.run(request, llm)
    raise ValueError(f"Unknown architecture '{architecture}' (expected 'single' or 'staged')")


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Assessment adapter - keep this signature; the public/hidden harness calls it."""
    return run_pipeline(get_request(request_id), architecture=architecture)
