# DC-Agent Codex Harness Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first workflow-driven Codex harness for DC-Agent so every Codex task has a repeatable intake, contract, implementation, verification, and review path.

**Architecture:** Keep the first version small and repo-native. Add one workflow guide, one machine-readable contract, one evaluator script, one unified agent check script, and CI wiring that runs the same local checks Codex uses.

**Tech Stack:** Bash, Python 3.12, pytest, uv, GitHub Actions, existing DC-Agent knowledge-base tests.

---

## Current Repo Context

- Existing agent guide: `AGENTS.md`
- Existing test runner: `scripts/run_pytests_ci.sh`
- Existing PR check runner: `scripts/pr_test_env.sh`
- Existing hygiene check: `scripts/check_clean.py`
- Existing knowledge-base tests: `tests/test_kb_import.py`, `tests/unit/test_sparse_retriever.py`, `tests/unit/test_faiss_vec_db.py`
- Missing harness docs for this plan: `docs/CODEX_WORKFLOW.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `docs/QUALITY.md`, `harness/README.md`
- Missing contract/evaluator layout: `harness/contracts/`, `harness/evaluator/`
- Missing unified Codex entry script: `scripts/agent-check.sh`

## Workflow Shape

Each Codex task should move through this workflow:

1. Intake: read `AGENTS.md`, `docs/CODEX_WORKFLOW.md`, and the relevant contract.
2. Contract: confirm the acceptance criteria and pick the narrowest verifier.
3. Implementation: make scoped changes only.
4. Verification: run `scripts/agent-check.sh --profile targeted` or `--profile full`.
5. Review: summarize changed files, commands run, failures fixed, and residual risk.

## File Structure

- Create `docs/CODEX_WORKFLOW.md`: human-readable workflow Codex follows for every task.
- Create `docs/ARCHITECTURE.md`: compact system map focused on files Codex should inspect first.
- Create `docs/DEVELOPMENT.md`: setup, local run, test commands, and safe data rules.
- Create `docs/QUALITY.md`: verification matrix and when to run each harness profile.
- Create `harness/README.md`: harness layout and ownership boundaries.
- Create `harness/contracts/knowledge_base_import.json`: first machine-readable acceptance contract.
- Create `harness/evaluator/__init__.py`: marks evaluator package.
- Create `harness/evaluator/kb_import_contract.py`: validates the contract shape and points to executable checks.
- Create `tests/harness/test_kb_import_contract.py`: unit tests for the contract evaluator.
- Create `scripts/agent-check.sh`: single local verification entry point for Codex.
- Create `.github/workflows/codex_harness.yml`: CI job that runs the contract and targeted checks.
- Modify `AGENTS.md`: add concise workflow and completion references.

---

### Task 1: Add The Codex Workflow Guide

**Files:**
- Create: `docs/CODEX_WORKFLOW.md`

- [ ] **Step 1: Create the workflow guide**

Create `docs/CODEX_WORKFLOW.md` with this content:

```md
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
```

- [ ] **Step 2: Verify the file exists**

Run:

```bash
test -f docs/CODEX_WORKFLOW.md
```

Expected: command exits with status `0`.

- [ ] **Step 3: Commit**

```bash
git add docs/CODEX_WORKFLOW.md
git commit -m "docs: add Codex workflow guide"
```

---

### Task 2: Add Compact Project Documentation

**Files:**
- Create: `docs/ARCHITECTURE.md`
- Create: `docs/DEVELOPMENT.md`
- Create: `docs/QUALITY.md`
- Create: `harness/README.md`

- [ ] **Step 1: Create `docs/ARCHITECTURE.md`**

```md
# Architecture

DC-Agent is based on AstrBot and extends it with company-local agent, knowledge-base, NAS, Feishu, dashboard, and harness capabilities.

## Primary Areas

| Area | Paths | Notes |
|---|---|---|
| Core lifecycle | `main.py`, `astrbot/core/core_lifecycle.py` | Starts services and initializes managers |
| Knowledge base | `astrbot/core/knowledge_base/` | Parses, chunks, stores, and retrieves documents |
| KB dashboard API | `astrbot/dashboard/routes/knowledge_base.py` | Upload, import, progress, retrieve endpoints |
| Agent KB tools | `astrbot/core/tools/knowledge_base_tools.py` | Runtime retrieval surface for agents |
| Harness | `harness/`, `tests/harness/` | Agent workflow support and evaluators |
| NAS sync | `nas_sync/` | Local document discovery and sync utilities |
| Dashboard | `dashboard/` | Vue-based management UI |

## Knowledge-Base Flow

```text
source file
  -> parser
  -> chunks
  -> document metadata
  -> sparse retriever and vector store
  -> retrieval result
  -> agent answer with source context
```

## Runtime Data Rule

Runtime data under `data/knowledge_base`, `data/temp`, `data/output`, logs, and local configs must not be committed.
```

- [ ] **Step 2: Create `docs/DEVELOPMENT.md`**

```md
# Development

## Setup

```bash
uv sync --group dev
```

## Run Core Server

```bash
uv run main.py
```

The API server listens on `http://localhost:6185` by default.

## Run Dashboard

```bash
cd dashboard
pnpm install
pnpm dev
```

The dashboard listens on `http://localhost:3000` by default.

## Common Checks

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest tests/test_kb_import.py tests/harness -q
scripts/agent-check.sh --profile targeted
```

## Safe Data Rules

- Do not commit secrets from `data/config/`.
- Do not commit runtime files from `data/knowledge_base/`, `data/temp/`, `data/output/`, `logs/`, or `tmp/`.
- Use fixture documents for tests instead of real NAS files.
```

- [ ] **Step 3: Create `docs/QUALITY.md`**

```md
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
```

- [ ] **Step 4: Create `harness/README.md`**

```md
# Harness

The harness directory contains workflow contracts and evaluators that make Codex work repeatable and verifiable.

## Layout

```text
harness/
  contracts/
    knowledge_base_import.json
  evaluator/
    kb_import_contract.py
```

## Contract Rules

Contracts define what completion means. They must include:

- `feature`
- `goal`
- `acceptance_criteria`
- `verification`

## Evaluator Rules

Evaluators should be deterministic, local, and safe to run in CI. They should validate contracts and route Codex to executable pytest or shell checks.
```

- [ ] **Step 5: Verify the docs exist**

Run:

```bash
test -f docs/ARCHITECTURE.md
test -f docs/DEVELOPMENT.md
test -f docs/QUALITY.md
test -f harness/README.md
```

Expected: all commands exit with status `0`.

- [ ] **Step 6: Commit**

```bash
git add docs/ARCHITECTURE.md docs/DEVELOPMENT.md docs/QUALITY.md harness/README.md
git commit -m "docs: add Codex harness foundations"
```

---

### Task 3: Add The First Knowledge-Base Contract

**Files:**
- Create: `harness/contracts/knowledge_base_import.json`
- Create: `harness/evaluator/__init__.py`
- Create: `harness/evaluator/kb_import_contract.py`
- Create: `tests/harness/test_kb_import_contract.py`

- [ ] **Step 1: Create `harness/contracts/knowledge_base_import.json`**

```json
{
  "feature": "knowledge_base_import",
  "goal": "Import documents into the DC-Agent knowledge base while preserving document identity, chunk content, progress reporting, and failure details.",
  "acceptance_criteria": [
    {
      "id": "kb-import-001",
      "description": "Pre-chunked TXT and Markdown documents can be imported through the dashboard API.",
      "verification": "uv run pytest tests/test_kb_import.py::test_import_documents -q"
    },
    {
      "id": "kb-import-002",
      "description": "Embedding or upload failures are reported per file without marking the whole task as crashed.",
      "verification": "uv run pytest tests/test_kb_import.py::test_import_documents_returns_friendly_failure_message -q"
    },
    {
      "id": "kb-import-003",
      "description": "Sparse and vector retrieval dependencies remain covered by unit tests.",
      "verification": "uv run pytest tests/unit/test_sparse_retriever.py tests/unit/test_faiss_vec_db.py -q"
    }
  ],
  "verification": {
    "targeted": "scripts/agent-check.sh --profile targeted",
    "full": "scripts/agent-check.sh --profile full"
  }
}
```

- [ ] **Step 2: Create `harness/evaluator/__init__.py`**

```python
"""Contract evaluators for the DC-Agent harness."""
```

- [ ] **Step 3: Create `harness/evaluator/kb_import_contract.py`**

```python
"""Validate and inspect the knowledge-base import harness contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT = REPO_ROOT / "harness" / "contracts" / "knowledge_base_import.json"
REQUIRED_TOP_LEVEL_KEYS = {"feature", "goal", "acceptance_criteria", "verification"}
REQUIRED_CRITERIA_KEYS = {"id", "description", "verification"}


def load_contract(path: Path = DEFAULT_CONTRACT) -> dict[str, Any]:
    with path.open(encoding="utf-8") as contract_file:
        data = json.load(contract_file)
    if not isinstance(data, dict):
        raise ValueError("contract root must be a JSON object")
    return data


def validate_contract(contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    missing = sorted(REQUIRED_TOP_LEVEL_KEYS - contract.keys())
    if missing:
        errors.append(f"missing top-level keys: {', '.join(missing)}")

    criteria = contract.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        errors.append("acceptance_criteria must be a non-empty list")
        return errors

    seen_ids: set[str] = set()
    for index, criterion in enumerate(criteria, start=1):
        if not isinstance(criterion, dict):
            errors.append(f"criterion {index} must be an object")
            continue

        missing_criterion_keys = sorted(REQUIRED_CRITERIA_KEYS - criterion.keys())
        if missing_criterion_keys:
            errors.append(
                f"criterion {index} missing keys: {', '.join(missing_criterion_keys)}"
            )

        criterion_id = criterion.get("id")
        if not isinstance(criterion_id, str) or not criterion_id:
            errors.append(f"criterion {index} id must be a non-empty string")
        elif criterion_id in seen_ids:
            errors.append(f"duplicate criterion id: {criterion_id}")
        else:
            seen_ids.add(criterion_id)

        verification = criterion.get("verification")
        if not isinstance(verification, str) or not verification.startswith("uv run "):
            errors.append(
                f"criterion {index} verification must be a uv run command"
            )

    return errors


def verification_commands(contract: dict[str, Any]) -> list[str]:
    return [
        criterion["verification"]
        for criterion in contract["acceptance_criteria"]
        if isinstance(criterion, dict) and isinstance(criterion.get("verification"), str)
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--contract",
        type=Path,
        default=DEFAULT_CONTRACT,
        help="Path to the contract JSON file.",
    )
    parser.add_argument(
        "--list-commands",
        action="store_true",
        help="Print verification commands from the contract.",
    )
    args = parser.parse_args()

    contract = load_contract(args.contract)
    errors = validate_contract(contract)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    if args.list_commands:
        for command in verification_commands(contract):
            print(command)
    else:
        print(f"Contract valid: {args.contract}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Create `tests/harness/test_kb_import_contract.py`**

```python
import json

from harness.evaluator.kb_import_contract import (
    DEFAULT_CONTRACT,
    load_contract,
    validate_contract,
    verification_commands,
)


def test_knowledge_base_import_contract_is_valid():
    contract = load_contract(DEFAULT_CONTRACT)

    assert validate_contract(contract) == []


def test_knowledge_base_import_contract_has_unique_criteria_ids():
    contract = load_contract(DEFAULT_CONTRACT)
    criterion_ids = [
        criterion["id"] for criterion in contract["acceptance_criteria"]
    ]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_knowledge_base_import_contract_points_to_pytest_verifiers():
    contract = load_contract(DEFAULT_CONTRACT)

    assert verification_commands(contract) == [
        "uv run pytest tests/test_kb_import.py::test_import_documents -q",
        "uv run pytest tests/test_kb_import.py::test_import_documents_returns_friendly_failure_message -q",
        "uv run pytest tests/unit/test_sparse_retriever.py tests/unit/test_faiss_vec_db.py -q",
    ]


def test_contract_validator_rejects_missing_required_keys(tmp_path):
    contract_path = tmp_path / "broken.json"
    contract_path.write_text(
        json.dumps({"feature": "knowledge_base_import"}),
        encoding="utf-8",
    )
    contract = load_contract(contract_path)

    errors = validate_contract(contract)

    assert "missing top-level keys: acceptance_criteria, goal, verification" in errors
```

- [ ] **Step 5: Run contract tests**

Run:

```bash
uv run pytest tests/harness/test_kb_import_contract.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Run evaluator manually**

Run:

```bash
uv run python -m harness.evaluator.kb_import_contract
uv run python -m harness.evaluator.kb_import_contract --list-commands
```

Expected: the first command prints `Contract valid: ...`; the second prints three `uv run pytest ...` commands.

- [ ] **Step 7: Commit**

```bash
git add harness/contracts/knowledge_base_import.json harness/evaluator tests/harness/test_kb_import_contract.py
git commit -m "test: add knowledge base import contract evaluator"
```

---

### Task 4: Add Unified Agent Check Script

**Files:**
- Create: `scripts/agent-check.sh`

- [ ] **Step 1: Create `scripts/agent-check.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PROFILE="targeted"
RUN_SYNC=false

usage() {
  cat <<'EOF'
Usage:
  scripts/agent-check.sh [options]

Options:
  --profile <targeted|full>  Verification profile. Default: targeted
  --sync                     Run uv sync before checks
  -h, --help                 Show this help message
EOF
}

while (($# > 0)); do
  case "$1" in
    --profile)
      PROFILE="${2:-}"
      if [[ "$PROFILE" != "targeted" && "$PROFILE" != "full" ]]; then
        echo "Unsupported profile: $PROFILE" >&2
        exit 1
      fi
      shift 2
      ;;
    --sync)
      RUN_SYNC=true
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

mkdir -p data/plugins data/config data/temp data/skills
export TESTING="${TESTING:-true}"
export ZHIPU_API_KEY="${ZHIPU_API_KEY:-test-api-key}"

if [[ "$RUN_SYNC" == true ]]; then
  echo "==> Syncing dependencies"
  uv sync --group dev
fi

echo "==> Checking repository hygiene"
uv run python scripts/check_clean.py

echo "==> Validating knowledge-base import contract"
uv run python -m harness.evaluator.kb_import_contract

echo "==> Running targeted harness tests"
uv run pytest \
  tests/harness/test_kb_import_contract.py \
  tests/test_kb_import.py::test_import_documents \
  tests/test_kb_import.py::test_import_documents_returns_friendly_failure_message \
  -q

if [[ "$PROFILE" == "full" ]]; then
  echo "==> Running format check"
  uv run ruff format --check .

  echo "==> Running lint check"
  uv run ruff check .

  echo "==> Running full pytest suite"
  uv run pytest --cov=. -v -o log_cli=true -o log_level=DEBUG

  echo "==> Running startup smoke check"
  uv run python scripts/smoke_startup_check.py
fi

echo "==> Agent checks completed successfully"
```

- [ ] **Step 2: Make the script executable**

Run:

```bash
chmod +x scripts/agent-check.sh
```

Expected: command exits with status `0`.

- [ ] **Step 3: Run targeted agent check**

Run:

```bash
scripts/agent-check.sh --profile targeted
```

Expected: contract validation and targeted tests pass.

- [ ] **Step 4: Commit**

```bash
git add scripts/agent-check.sh
git commit -m "chore: add unified agent check script"
```

---

### Task 5: Update `AGENTS.md` With Workflow Entry Points

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Add workflow guidance to `AGENTS.md`**

Append this section after the existing setup and hygiene guidance:

```md
## Codex workflow

Before code changes, read:

1. `docs/CODEX_WORKFLOW.md`
2. The relevant contract in `harness/contracts/`
3. The closest tests under `tests/`

Use the targeted profile for narrow changes:

```bash
scripts/agent-check.sh --profile targeted
```

Use the full profile before PRs or broad backend changes:

```bash
scripts/agent-check.sh --profile full
```

For knowledge-base import work, the first contract is:

```text
harness/contracts/knowledge_base_import.json
```
```

- [ ] **Step 2: Check `AGENTS.md` renders cleanly**

Run:

```bash
python - <<'PY'
from pathlib import Path
text = Path("AGENTS.md").read_text(encoding="utf-8")
assert "## Codex workflow" in text
assert "scripts/agent-check.sh --profile targeted" in text
PY
```

Expected: command exits with status `0`.

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs: document Codex workflow entry points"
```

---

### Task 6: Add CI For The Harness Workflow

**Files:**
- Create: `.github/workflows/codex_harness.yml`

- [ ] **Step 1: Create `.github/workflows/codex_harness.yml`**

```yaml
name: Codex Harness

on:
  pull_request:
    paths:
      - 'AGENTS.md'
      - 'docs/CODEX_WORKFLOW.md'
      - 'docs/ARCHITECTURE.md'
      - 'docs/DEVELOPMENT.md'
      - 'docs/QUALITY.md'
      - 'harness/**'
      - 'scripts/agent-check.sh'
      - 'tests/harness/**'
      - 'tests/test_kb_import.py'
      - '.github/workflows/codex_harness.yml'
  workflow_dispatch:

jobs:
  codex-harness:
    name: Run targeted Codex harness checks
    runs-on: ubuntu-latest
    timeout-minutes: 20

    steps:
      - name: Checkout
        uses: actions/checkout@v6

      - name: Set up Python
        uses: actions/setup-python@v6
        with:
          python-version: '3.12'

      - name: Install uv
        run: |
          python -m pip install --upgrade pip
          python -m pip install uv

      - name: Sync dependencies
        run: uv sync --group dev

      - name: Run targeted Codex harness
        run: bash scripts/agent-check.sh --profile targeted
```

- [ ] **Step 2: Validate workflow YAML exists**

Run:

```bash
test -f .github/workflows/codex_harness.yml
```

Expected: command exits with status `0`.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/codex_harness.yml
git commit -m "ci: add Codex harness workflow"
```

---

### Task 7: Final Verification

**Files:**
- Read: all files changed in Tasks 1-6

- [ ] **Step 1: Run the targeted profile**

Run:

```bash
scripts/agent-check.sh --profile targeted
```

Expected: all targeted checks pass.

- [ ] **Step 2: Run repository hygiene check**

Run:

```bash
make check-clean
```

Expected: `Repository hygiene check passed.`

- [ ] **Step 3: Inspect git diff**

Run:

```bash
git diff --stat
git diff -- AGENTS.md docs/CODEX_WORKFLOW.md docs/ARCHITECTURE.md docs/DEVELOPMENT.md docs/QUALITY.md harness/README.md harness/contracts/knowledge_base_import.json harness/evaluator/kb_import_contract.py tests/harness/test_kb_import_contract.py scripts/agent-check.sh .github/workflows/codex_harness.yml
```

Expected: diff contains only harness workflow, docs, contract, evaluator, script, and CI changes.

- [ ] **Step 4: Final response**

Summarize:

- Workflow added
- Contract added
- Commands run
- Test result
- Remaining risk

Use this final response format:

```text
Implemented the first DC-Agent Codex harness workflow.

Changed:
- ...

Verified:
- ...

Remaining risk:
- ...
```

---

## Self-Review

- Spec coverage: The plan covers `AGENTS.md`, docs, contract, evaluator, unified check script, CI, and verification.
- Placeholder scan: No task relies on placeholder implementation.
- Type consistency: The evaluator functions used by tests are defined in the same task.
- Scope: This plan avoids changing business logic and uses existing KB tests as the first harness verifier.
