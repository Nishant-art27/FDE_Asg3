"""Product UI - AI Procurement Request Copilot (Streamlit).

Three panels: request details | evidence | recommendation + human action.
The copilot recommends; a human records the decision (data/review_log.jsonl).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests
import streamlit as st

from src.contracts import ProcurementDecision
from src.data_access import load_requests
from src.llm import LLM
from src.review_log import HUMAN_ACTIONS, load_log, record_decision
from src.solution import run_pipeline

ROOT = Path(__file__).resolve().parent
REQUESTS = load_requests()
BY_ID = {r["request_id"]: r for r in REQUESTS}
API_HEALTH = os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001").rstrip("/") + "/health"

st.set_page_config(page_title="Procurement Request Copilot", layout="wide", page_icon="🧾")
st.title("AI Procurement Request Copilot")
st.caption("AI interprets context and recommends · code enforces thresholds and deterministic checks · humans approve.")


@st.cache_resource
def get_llm() -> LLM:
    return LLM()


def api_up() -> bool:
    try:
        return requests.get(API_HEALTH, timeout=0.5).ok
    except requests.RequestException:
        return False


# ----------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Run")
    llm = get_llm()
    st.markdown(f"**Model:** `{llm.model}`  \n**Mode:** `{llm.provider}`" +
                ("" if llm.available else "  \n:orange[Offline: set GROQ_API_KEY in .env for model-backed runs]"))
    st.markdown(f"**Vendor-risk API:** {'🟢 up' if api_up() else '🔴 down (run `python run_local.py`)'}")
    architecture = st.radio("Architecture", ["single", "staged"], horizontal=True,
                            help="single = one agent drives tools; staged = code gathers evidence, Analyst + Reviewer agents")
    source = st.radio("Request source", ["Existing request", "New request"], horizontal=True)
    if source == "Existing request":
        request_id = st.selectbox("Request", list(BY_ID), format_func=lambda r: f"{r} · {BY_ID[r]['product_name']}")
        request = dict(BY_ID[request_id])
    else:
        st.caption("Compose a request (all fields are treated as untrusted data).")
        request = {
            "request_id": st.text_input("Request id", "REQ-NEW-001"),
            "requester_id": st.text_input("Requester id", "E001"),
            "product_name": st.text_input("Product", ""),
            "vendor_name": st.text_input("Vendor", ""),
            "category": st.text_input("Category", ""),
            "annual_cost_usd": st.number_input("Annual cost (USD, 0 = unknown)", min_value=0.0, value=0.0, step=100.0),
            "user_count": st.number_input("Users (0 = unknown)", min_value=0, value=0, step=1),
            "business_justification": st.text_area("Business justification", ""),
            "data_access_level": st.selectbox("Data access level", ["unknown", "none", "internal_documents", "internal_marketing",
                                                                     "confidential_documents", "source_code", "production_telemetry",
                                                                     "employee_pii", "customer_pii"]),
            "requested_integrations": [s.strip() for s in st.text_input("Integrations (comma separated)", "").split(",") if s.strip()],
            "urgency": st.selectbox("Urgency", ["normal", "high", "urgent"]),
        }
        if request["annual_cost_usd"] == 0:
            request["annual_cost_usd"] = None
        if request["user_count"] == 0:
            request["user_count"] = None
    run_clicked = st.button("Run analysis", type="primary", use_container_width=True)

# ----------------------------------------------------------------------------- run
state_key = f"{request.get('request_id')}::{architecture}"
if run_clicked:
    with st.spinner(f"Running {architecture} architecture..."):
        t0 = time.perf_counter()
        try:
            decision = run_pipeline(request, architecture, llm=llm)
            st.session_state["last"] = (state_key, decision, time.perf_counter() - t0)
        except Exception as exc:  # surface, never hide
            st.session_state["last"] = (state_key, exc, time.perf_counter() - t0)

col_req, col_ev, col_rec = st.columns([0.9, 1.3, 1.1], gap="large")

with col_req:
    st.subheader("1 · Request details")
    st.json(request, expanded=True)

last = st.session_state.get("last")
decision: ProcurementDecision | None = None
if last and last[0] == state_key:
    if isinstance(last[1], Exception):
        st.error(f"Run failed: {type(last[1]).__name__}: {last[1]}")
    else:
        decision = last[1]

with col_ev:
    st.subheader("2 · Evidence panel")
    if decision is None:
        st.info("Run the analysis to gather evidence.")
    else:
        tel = decision.telemetry
        if tel:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("LLM calls", tel.llm_calls)
            m2.metric("Tool calls", tel.tool_calls)
            m3.metric("Latency", f"{(tel.latency_ms or 0) / 1000:.1f}s")
            m4.metric("Tokens in/out", f"{tel.input_tokens or 0}/{tel.output_tokens or 0}")
        for e in decision.evidence:
            with st.container(border=True):
                st.markdown(f"**{e.source}**  \n{e.finding}")
                if e.reference:
                    st.caption(f"ref: {e.reference}")
        if tel:
            with st.expander("Tool trace & guardrails"):
                st.write("Tools called:", ", ".join(tel.tool_names))
                st.write("Mode:", tel.llm_mode, "· model:", tel.model)
                st.write("Stage latency (ms):", tel.stage_latency_ms)
                st.write("Guardrail overrides:", tel.guardrail_overrides or "none")

with col_rec:
    st.subheader("3 · Recommendation & action")
    if decision is None:
        st.info("No recommendation yet.")
    else:
        colour = {"Approve": "green", "Route for review": "orange", "Use existing tool": "blue",
                  "Request clarification": "violet", "Manual review - evidence unavailable": "red"}.get(decision.recommendation, "gray")
        st.markdown(f"### :{colour}[{decision.recommendation}]")
        if decision.summary:
            st.write(decision.summary)
        st.markdown("**Approvals required**")
        st.write(", ".join(decision.required_approvals) or "—")
        st.markdown("**Risk flags**")
        st.write(" ".join(f"`{f}`" for f in decision.risk_flags) or "none")
        st.markdown("**Missing information**")
        st.write("\n".join(f"- {m}" for m in decision.missing_information) or "none")
        st.markdown("**Next step**")
        st.write(decision.next_step)
        st.caption("Human review required: the copilot never approves, purchases or changes budgets.")

        st.divider()
        st.markdown("**Human decision (handoff)**")
        with st.form("human_decision"):
            reviewer = st.text_input("Reviewer", "procurement@company")
            action = st.selectbox("Action", HUMAN_ACTIONS)
            note = st.text_area("Note", "")
            if st.form_submit_button("Record decision"):
                entry = record_decision(decision.request_id, architecture, decision.recommendation, action, reviewer, note)
                st.success(f"Recorded: {entry['human_action']} by {entry['reviewer']}")
        history = load_log(decision.request_id)
        if history:
            with st.expander(f"Decision history ({len(history)})"):
                st.table(history)
        with st.expander("Raw ProcurementDecision JSON"):
            st.code(json.dumps(decision.model_dump(), indent=2, default=str), language="json")
