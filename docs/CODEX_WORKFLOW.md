# Codex Workflow

DC-Agent uses a workflow-driven harness. Codex must move every code task through intake, contract, implementation, verification, and review.

## 1. Intake

Read these files before changing code:

- `AGENTS.md`
- `docs/CODEX_WORKFLOW.md`
- The contract in `harness/contracts/` that matches the feature area
- The closest tests under `tests/`

For knowledge-base work, also inspect:

- `astrbot/core/knowledge_base/`
- `astrbot/dashboard/routes/knowledge_base.py`
- `astrbot/core/tools/knowledge_base_tools.py`
- `tests/test_kb_import.py`

## 2. Contract

Before implementation, identify the acceptance criteria that must pass. If no contract exists for the area, create the smallest useful contract before changing behavior.

## 3. Implementation

Keep changes scoped to the requested behavior. Do not edit runtime data, secrets, or generated artifacts. Prefer existing project patterns over new frameworks.

## 4. Verification

Run the narrowest check that proves the change:

```bash
scripts/agent-check.sh --profile targeted
```

Run the full check before PRs or cross-module changes:

```bash
scripts/agent-check.sh --profile full
```

## 5. Review Summary

Final Codex responses must include:

- Changed files
- Commands run
- Test result
- Remaining risk

## Profiles

| Profile | Purpose | Command |
|---|---|---|
| targeted | Fast local proof for harness and KB import contracts | `scripts/agent-check.sh --profile targeted` |
| full | Lint, pytest, smoke check, and dashboard build when available | `scripts/agent-check.sh --profile full` |
