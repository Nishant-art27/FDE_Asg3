"""Human handoff: reviewers record their decision on a copilot recommendation.
The copilot never approves; this log is the audit trail of what humans decided."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "data" / "review_log.jsonl"

HUMAN_ACTIONS = ["Approve recommendation", "Send to reviewers", "Request clarification from requester", "Reject request"]


def record_decision(request_id: str, architecture: str, recommendation: str, action: str,
                    reviewer: str, note: str = "") -> dict:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request_id": request_id,
        "architecture": architecture,
        "copilot_recommendation": recommendation,
        "human_action": action,
        "reviewer": reviewer,
        "note": note,
    }
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def load_log(request_id: str | None = None) -> list[dict]:
    if not LOG_PATH.exists():
        return []
    rows = [json.loads(line) for line in LOG_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if request_id is None or r["request_id"] == request_id]
