# Evaluation results

Generated 2026-10-11T17:37:45+00:00 - 10 cases x 1 repeat(s) per architecture.
Model: `llama-3.3-70b-versatile` - mode: `offline`.

| Metric | single | staged |
|---|---:|---:|
| Cases passing ALL quality criteria | 10/10 | 10/10 |
| Public-harness minimum checks passed | 10/10 | 10/10 |
| Correct recommendation / next action | 10/10 | 10/10 |
| Evidence grounded in tool results | 10/10 | 10/10 |
| Policy + deterministic rules followed | 10/10 | 10/10 |
| Escalation / human review correct | 10/10 | 10/10 |
| Avg latency (ms) | 27 | 26 |
| Avg LLM calls | 0.00 | 0.00 |
| Avg tool calls | 7.00 | 7.00 |
| Avg tokens (in / out) | 0 / 0 | 0 / 0 |
| Runs where guardrails had to correct the model | 0/10 | 0/10 |
| Runs with ungrounded evidence dropped | 0/10 | 0/10 |

## Per-case results

| Case | Arch | Rep | Recommendation | Action | Grounded | Policy | Escalation | Latency ms | LLM | Tools | Overrides / notes |
|---|---|---:|---|:-:|:-:|:-:|:-:|---:|---:|---:|---|
| PUB-01 | single | 1 | Approve | ✅ | ✅ | ✅ | ✅ | 12 | 0 | 7 | llm_unavailable:offline mode |
| PUB-02 | single | 1 | Route for review | ✅ | ✅ | ✅ | ✅ | 6 | 0 | 7 | llm_unavailable:offline mode |
| PUB-03 | single | 1 | Route for review | ✅ | ✅ | ✅ | ✅ | 5 | 0 | 7 | llm_unavailable:offline mode |
| PUB-04 | single | 1 | Route for review | ✅ | ✅ | ✅ | ✅ | 4 | 0 | 7 | llm_unavailable:offline mode |
| PUB-05 | single | 1 | Request clarification | ✅ | ✅ | ✅ | ✅ | 4 | 0 | 7 | llm_unavailable:offline mode |
| PUB-06 | single | 1 | Manual review - evidence unavailable | ✅ | ✅ | ✅ | ✅ | 215 | 0 | 7 | llm_unavailable:offline mode |
| EXT-07 | single | 1 | Route for review | ✅ | ✅ | ✅ | ✅ | 9 | 0 | 7 | llm_unavailable:offline mode |
| EXT-08 | single | 1 | Route for review | ✅ | ✅ | ✅ | ✅ | 7 | 0 | 7 | llm_unavailable:offline mode |
| EXT-09 | single | 1 | Use existing tool | ✅ | ✅ | ✅ | ✅ | 6 | 0 | 7 | llm_unavailable:offline mode |
| EXT-10 | single | 1 | Approve | ✅ | ✅ | ✅ | ✅ | 5 | 0 | 7 | llm_unavailable:offline mode |
| PUB-01 | staged | 1 | Approve | ✅ | ✅ | ✅ | ✅ | 5 | 0 | 7 | llm_unavailable:offline mode |
| PUB-02 | staged | 1 | Route for review | ✅ | ✅ | ✅ | ✅ | 4 | 0 | 7 | llm_unavailable:offline mode |
| PUB-03 | staged | 1 | Route for review | ✅ | ✅ | ✅ | ✅ | 4 | 0 | 7 | llm_unavailable:offline mode |
| PUB-04 | staged | 1 | Route for review | ✅ | ✅ | ✅ | ✅ | 4 | 0 | 7 | llm_unavailable:offline mode |
| PUB-05 | staged | 1 | Request clarification | ✅ | ✅ | ✅ | ✅ | 4 | 0 | 7 | llm_unavailable:offline mode |
| PUB-06 | staged | 1 | Manual review - evidence unavailable | ✅ | ✅ | ✅ | ✅ | 216 | 0 | 7 | llm_unavailable:offline mode |
| EXT-07 | staged | 1 | Route for review | ✅ | ✅ | ✅ | ✅ | 8 | 0 | 7 | llm_unavailable:offline mode |
| EXT-08 | staged | 1 | Route for review | ✅ | ✅ | ✅ | ✅ | 7 | 0 | 7 | llm_unavailable:offline mode |
| EXT-09 | staged | 1 | Use existing tool | ✅ | ✅ | ✅ | ✅ | 6 | 0 | 7 | llm_unavailable:offline mode |
| EXT-10 | staged | 1 | Approve | ✅ | ✅ | ✅ | ✅ | 5 | 0 | 7 | llm_unavailable:offline mode |
