# Architecture Decision Memo

## Decision

**Ship Architecture B - staged / 2-agent**: code gathers evidence and runs the policy engine, an Analyst agent writes the package, a Reviewer agent may only tighten it. Architecture A (single agent) stays as the baseline.

## Evidence

Same 10 cases (6 public + 4 edge), both architectures, identical guardrails. Tables: `docs/evaluation_results.md`; rows: `evals/results_comparison.csv`.

| Metric | Single agent (A) | Staged / 2-agent (B) |
|---|---:|---:|
| Cases passing all quality criteria | see results file | see results file |
| Avg latency | see results file | see results file |
| Avg LLM calls | 2-4 (model-chosen tool rounds) | exactly 2 |
| Avg tool calls | model-chosen, gaps auto-filled | 7, fixed |
| Policy / grounding failures in output | 0 (guardrails) | 0 (guardrails) |
| Runs where guardrails corrected the model | see results file | see results file |

Deterministic floor (offline mode, 0 model calls): both pass 10/10 on every criterion in ~26 ms. Because thresholds, budget, expiry, conflict, outage and injection rules live in code, the model cannot make the product *wrong* on those dimensions, only better or more expensive.

## Trade-offs

**B improves:** the model never decides *whether* to look at evidence. All seven tools run in fixed order, so the evidence panel is complete and identical across runs and token use is predictable. The Reviewer removes ungrounded evidence and missed flags before the guardrails do. Two calls is the floor for a written package plus an independent check.

**A does better:** one prompt, one loop, can skip irrelevant tools. But the policy engine needs all six sources anyway: the agent either calls them all (same cost as B, more turns) or skips some and the policy tool fetches them (`policy_autofetched`). Tool selection buys nothing here.

**Cost of B:** two prompts to maintain and one extra call of latency. If measured Reviewer corrections stay near zero, drop it; `with_reviewer=False` is already supported.

## Risks / limitations

- Overlap judgement (policy s3) is the only policy decision delegated to the model; validate on more add-on vs. duplicate cases.
- Injection detection is regex plus model judgement; red-team paraphrased instructions in vendor notes.
- Ten cases is a smoke test; rerun with `--repeats 3` on hidden-style cases and track `guardrail_overrides` as the quality signal.
- Provider latency and JSON-mode support vary by model; provider failure degrades to deterministic mode (safe, but should alert).

## Why this is the right MVP

The client needs *reliable* evidence and a defensible recommendation humans act on. Reliability comes from code: budget arithmetic, thresholds, 365-day expiry, conflict detection and outage handling are deterministic and tested. The model's job is narrow: interpret the justification, judge overlap, write the summary and next step. B gives that job complete evidence every time with the fewest model calls; the Reviewer is a cheap, removable check, not orchestration for its own sake. More agents add no evidence; fewer calls remove the independent review.
