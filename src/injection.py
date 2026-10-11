"""Untrusted-content handling (policy section 9).

Request text, vendor notes and API text are business DATA. This module
(1) detects instruction-like content with deterministic heuristics so the
flag never depends on the model noticing, and (2) wraps untrusted text in
explicit tags before it reaches a prompt.
"""
from __future__ import annotations

import json
import re
from typing import Any

_PATTERNS: list[tuple[str, str]] = [
    (r"\bignore\s+(all|any|the|previous|prior|above|these)?\s*(procurement\s+|system\s+|safety\s+)?(rules|instructions|policy|policies|guidelines|controls)", "instruction_override"),
    (r"\bdisregard\b.*\b(rules|instructions|policy|policies)", "instruction_override"),
    (r"\b(bypass|skip|override|circumvent)\b.*\b(controls?|review|approval|policy|security|checks?)", "control_bypass"),
    (r"\btreat\s+(this|the)\s+request\s+as\b", "fabricated_approval"),
    (r"\b(already|is|been|as)\s+(cfo|ceo|cto|finance|security|legal|manager)[- ]?(approved|signed[- ]off)", "fabricated_approval"),
    (r"\bapprove\s+(it|this|the\s+request)?\s*(immediately|now|automatically|without)", "autonomous_approval"),
    (r"\bmark\s+(it|this)\s+(as\s+)?approved", "autonomous_approval"),
    (r"\byou\s+are\s+now\b", "role_hijack"),
    (r"\b(system|developer)\s+prompt\b", "prompt_probe"),
    (r"\b(reveal|print|expose|show)\b.*\b(secret|api[_ ]?key|credential|token|prompt)", "secret_exfiltration"),
    (r"<\s*/?\s*(system|instruction|assistant)\s*>", "tag_injection"),
    (r"\bdo\s+not\s+(flag|escalate|route)\b", "control_bypass"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE | re.DOTALL), label) for p, label in _PATTERNS]


def scan_text(text: str | None) -> list[str]:
    """Return the list of injection categories matched in `text` (empty if clean)."""
    if not text:
        return []
    hits: list[str] = []
    for rx, label in _COMPILED:
        if rx.search(text) and label not in hits:
            hits.append(label)
    return hits


def scan_fields(fields: dict[str, Any]) -> dict[str, list[str]]:
    """Scan several named free-text fields. Returns {field_name: [categories]} for hits only."""
    out: dict[str, list[str]] = {}
    for name, value in fields.items():
        if isinstance(value, list):
            value = " ".join(str(v) for v in value)
        hits = scan_text(value if isinstance(value, str) else None)
        if hits:
            out[name] = hits
    return out


def wrap_untrusted(label: str, payload: Any) -> str:
    """Serialise untrusted business data inside explicit tags for a prompt."""
    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
    # Neutralise any closing tag smuggled inside the data itself.
    payload = payload.replace("</untrusted_business_data>", "</untrusted_business_data_escaped>")
    return (
        f'<untrusted_business_data label="{label}">\n{payload}\n</untrusted_business_data>'
    )
