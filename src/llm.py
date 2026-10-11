"""Model layer: Groq chat completions (tool calling + JSON output) with an
explicit offline mode. Every call is counted; failures surface as
`LLMUnavailable` so the pipeline can degrade to deterministic mode instead
of crashing or fabricating.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

DEFAULT_MODEL = "openai/gpt-oss-120b"


class LLMUnavailable(Exception):
    """Raised when the model provider cannot be used (auth, network, quota, bad output)."""


@dataclass
class LLMCallRecord:
    stage: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    tool_calls: int = 0
    rate_limit_wait_ms: float = 0.0   # time spent waiting on 429 retry-after before this call succeeded
    note: str = ""


_RETRY_RX = re.compile(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)(ms|s)", re.IGNORECASE)


def retry_after_seconds(exc: Exception, default: float = 10.0, cap: float = 120.0) -> float:
    """Seconds to wait after a 429: Retry-After header, else the 'try again in 1m2.5s' message."""
    resp = getattr(exc, "response", None)
    header = resp.headers.get("retry-after") if resp is not None else None
    if header:
        try:
            return min(cap, max(0.5, float(header)))
        except ValueError:
            pass
    m = _RETRY_RX.search(str(getattr(exc, "message", "") or exc))
    if m:
        minutes, value, unit = m.groups()
        secs = float(value) / (1000.0 if unit == "ms" else 1.0) + (int(minutes) * 60 if minutes else 0)
        return min(cap, max(0.5, secs + 0.5))
    return default


ToolExecutor = Callable[[str, dict], Any]


def _extract_json(text: str) -> dict:
    """Parse a JSON object from model text (tolerates code fences / prose around it)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def recover_failed_generation(exc: Exception, tool_names: set[str]) -> tuple[str | None, str | None]:
    """Groq returns 400 `tool_use_failed` with the raw generation when a model emits a tool call it
    cannot validate. gpt-oss models often do this for the FINAL answer, calling a pseudo-tool named
    `json` whose arguments are the answer. Returns (final_json_text, attempted_tool_name)."""
    body = getattr(exc, "body", None)
    err = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(err, dict) or err.get("code") != "tool_use_failed":
        return None, None
    raw = err.get("failed_generation") or ""
    try:
        gen = _extract_json(raw)
    except (ValueError, TypeError):
        return None, None
    name = gen.get("name") if isinstance(gen, dict) else None
    args = gen.get("arguments") if isinstance(gen, dict) else None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = None
    if name not in tool_names and isinstance(args, dict):
        return json.dumps(args), name
    return None, name


class LLM:
    def __init__(self, provider: str | None = None, model: str | None = None, temperature: float | None = None):
        self.provider = (provider or os.getenv("LLM_PROVIDER") or "groq").strip().lower()
        self.model = model or os.getenv("MODEL_NAME") or DEFAULT_MODEL
        self.temperature = float(os.getenv("LLM_TEMPERATURE", "0") if temperature is None else temperature)
        self.reasoning_effort = (os.getenv("LLM_REASONING_EFFORT") or "").strip() or None  # low | medium | high (gpt-oss)
        self.max_rate_retries = int(os.getenv("LLM_MAX_RATE_RETRIES", "6"))
        self._client = None
        if self.provider == "groq":
            key = os.getenv("GROQ_API_KEY", "").strip()
            if not key:
                self.provider = "offline"
                self.unavailable_reason = "GROQ_API_KEY is not set"
            else:
                import groq
                # max_retries=0: transient errors and 429s are handled explicitly below so waits are measured
                self._client = groq.Groq(api_key=key, timeout=90.0, max_retries=0)
        elif self.provider != "offline":
            raise ValueError(f"Unsupported LLM_PROVIDER '{self.provider}' (use groq or offline)")

    @property
    def available(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------
    def chat(self, *, stage: str, system: str, user: str, schema_model: type[BaseModel],
             tools: list[dict] | None = None, tool_executor: ToolExecutor | None = None,
             max_rounds: int = 8) -> tuple[BaseModel, list[LLMCallRecord], list[dict]]:
        """Run one stage. With `tools`, drives the tool-calling loop until the model
        answers; the final answer is parsed and validated against `schema_model`.
        Returns (parsed, call records, message trace)."""
        if not self.available:
            raise LLMUnavailable(getattr(self, "unavailable_reason", "offline mode"))
        import groq

        schema = schema_model.model_json_schema()
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        records: list[LLMCallRecord] = []
        groq_tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                         "parameters": t["parameters"]}} for t in (tools or [])]

        def _create(**kwargs):
            if self.reasoning_effort:
                kwargs.setdefault("reasoning_effort", self.reasoning_effort)
            waited_ms = 0.0
            rate_attempts = 0
            transient_attempts = 0
            while True:
                start = time.perf_counter()
                try:
                    resp = self._client.chat.completions.create(model=self.model, temperature=self.temperature,
                                                                messages=messages, max_completion_tokens=8192, **kwargs)
                    break
                except (groq.AuthenticationError, groq.PermissionDeniedError) as exc:
                    raise LLMUnavailable(f"authentication failed: {exc.__class__.__name__}") from exc
                except groq.RateLimitError as exc:
                    rate_attempts += 1
                    if rate_attempts > self.max_rate_retries:
                        raise LLMUnavailable(f"rate limited after {rate_attempts - 1} retries: {exc.message[:120]}") from exc
                    wait = retry_after_seconds(exc)
                    time.sleep(wait)
                    waited_ms += wait * 1000
                except (groq.APIConnectionError, groq.APITimeoutError, groq.InternalServerError) as exc:
                    transient_attempts += 1
                    if transient_attempts > 2:
                        raise LLMUnavailable(f"provider unreachable: {exc.__class__.__name__}") from exc
                    time.sleep(2.0 * transient_attempts)
                except groq.BadRequestError:
                    raise
                except groq.APIStatusError as exc:
                    raise LLMUnavailable(f"provider error {exc.status_code}: {exc.message}") from exc
            latency = (time.perf_counter() - start) * 1000
            usage = getattr(resp, "usage", None)
            msg = resp.choices[0].message
            records.append(LLMCallRecord(stage=stage, model=self.model,
                                         input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                                         output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                                         latency_ms=latency, tool_calls=len(msg.tool_calls or []),
                                         rate_limit_wait_ms=waited_ms))
            return msg

        # ---- tool loop ---------------------------------------------------
        final_text: str | None = None
        tool_names = {t["function"]["name"] for t in groq_tools}
        executed: set[str] = set()
        if groq_tools:
            for _ in range(max_rounds):
                if "run_policy_checks" in executed:
                    # Evidence gathering is over (policy engine ran): ask for the structured answer without tools.
                    final_text = self._json_answer(_create, schema, schema_name=schema_model.__name__)
                    messages.append({"role": "assistant", "content": final_text})
                    break
                try:
                    msg = _create(tools=groq_tools, tool_choice="auto")
                except groq.BadRequestError as exc:
                    recovered, attempted = recover_failed_generation(exc, tool_names)
                    if recovered is not None:
                        final_text = recovered
                        messages.append({"role": "assistant", "content": final_text})
                        records[-1].note = "final answer recovered from tool_use_failed" if records else ""
                        break
                    if attempted is not None:
                        messages.append({"role": "user", "content":
                                         f"Your last tool call ('{attempted}') was invalid. Call only the listed tools "
                                         "with valid JSON arguments, or give the final JSON answer."})
                        continue
                    raise LLMUnavailable(f"bad request: {exc.message[:160]}") from exc
                if msg.tool_calls:
                    messages.append({"role": "assistant", "content": msg.content or "",
                                     "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
                    for tc in msg.tool_calls:
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        result = tool_executor(tc.function.name, args) if tool_executor else {"error": "no executor"}
                        executed.add(tc.function.name)
                        messages.append({"role": "tool", "tool_call_id": tc.id, "name": tc.function.name,
                                         "content": json.dumps(result, default=str)})
                    continue
                final_text = msg.content or ""
                messages.append({"role": "assistant", "content": final_text})
                break
            if final_text is None:
                # Ran out of rounds: ask for the final answer without tools.
                messages.append({"role": "user", "content": "Stop calling tools and give the final JSON answer now."})
                final_text = self._json_answer(_create, schema, schema_name=schema_model.__name__)
                messages.append({"role": "assistant", "content": final_text})
        else:
            final_text = self._json_answer(_create, schema, schema_name=schema_model.__name__)
            messages.append({"role": "assistant", "content": final_text})

        # ---- parse / validate (one repair attempt) ------------------------
        try:
            return schema_model.model_validate(_extract_json(final_text)), records, messages
        except (ValueError, ValidationError) as exc:
            messages.append({"role": "user", "content":
                             "Your previous answer was not valid for the required schema "
                             f"({type(exc).__name__}: {str(exc)[:300]}). Return ONLY a JSON object matching this schema:\n"
                             + json.dumps(schema)})
            repaired = self._json_answer(_create, schema, schema_name=schema_model.__name__)
            messages.append({"role": "assistant", "content": repaired})
            try:
                return schema_model.model_validate(_extract_json(repaired)), records, messages
            except (ValueError, ValidationError) as exc2:
                raise LLMUnavailable(f"model output did not match schema after repair: {exc2}") from exc2

    def _json_answer(self, create, schema: dict, schema_name: str) -> str:
        import groq
        attempts = [
            {"response_format": {"type": "json_schema", "json_schema": {"name": schema_name, "schema": schema}}},
            {"response_format": {"type": "json_object"}},   # model lacks json_schema support
            {},                                             # plain text; parsed leniently afterwards
        ]
        last_exc: Exception | None = None
        for kwargs in attempts:
            try:
                msg = create(**kwargs)
                return msg.content or "{}"
            except groq.BadRequestError as exc:
                last_exc = exc
                recovered, _ = recover_failed_generation(exc, set())
                if recovered is not None:
                    return recovered
        raise LLMUnavailable(f"bad request on final answer: {getattr(last_exc, 'message', last_exc)!s:.160}")
