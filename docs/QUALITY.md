# Quality

## Verification Matrix

| Change Type | Required Checks |
|---|---|
| Harness docs or contracts | `scripts/agent-check.sh --profile targeted` |
| Knowledge-base import | `uv run pytest tests/test_kb_import.py tests/harness -q` |
| Retrieval changes | `uv run pytest tests/unit/test_sparse_retriever.py tests/unit/test_faiss_vec_db.py -q` |
| API route changes | Closest route tests plus `scripts/agent-check.sh --profile targeted` |
| Cross-module backend changes | `scripts/agent-check.sh --profile full` |
| Dashboard changes | Dashboard build and relevant UI tests |

## Completion Standard

A Codex task is complete only when:

- The relevant contract criteria are satisfied.
- The selected checks pass.
- The final response lists changed files, commands run, and remaining risk.
- No runtime data, secrets, or generated caches are staged.
