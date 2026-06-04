## Setup commands

### Core

```
uv sync
uv run main.py
```

Exposed an API server on `http://localhost:6185` by default.

### Dashboard(WebUI)

```
cd dashboard
pnpm install # First time only. Use npm install -g pnpm if pnpm is not installed.
pnpm dev
```

Runs on `http://localhost:3000` by default.

## Pre-commit setup

AstrBot uses [pre-commit](https://pre-commit.com/) hooks to automatically format and lint Python code before each commit. The hooks run `ruff check`, `ruff format`, and `pyupgrade` (see [`.pre-commit-config.yaml`](.pre-commit-config.yaml) for details).

To set it up:

```bash
pip install pre-commit
pre-commit install
```

After installation, the hooks will run automatically on `git commit`. You can also run them manually at any time:

```bash
ruff format .
ruff check .
```

> **Note:** If you use VSCode, install the `Ruff` extension for real-time formatting and linting in the editor.

## Dev environment tips

1. When modifying the WebUI, be sure to maintain componentization and clean code. Avoid duplicate code.
2. Do not add any report files such as xxx_SUMMARY.md.
3. After finishing, use `ruff format .` and `ruff check .` to format and check the code.
4. When committing, ensure to use conventional commits messages, such as `feat: add new agent for data analysis` or `fix: resolve bug in provider manager`.
5. Use English for all new comments.
6. For path handling, use `pathlib.Path` instead of string paths, and use `astrbot.core.utils.path_utils` to get the AstrBot data and temp directory.

## DC-Agent hygiene checklist

Before committing DC-Agent changes:

1. Run `make clean-pyc` to remove Python bytecode and local test caches from code directories.
2. Run `make check-clean` and fix any reported generated, backup, or runtime files.
3. Keep runtime data out of git. Code under `data/plugins/` may be tracked; databases, user tokens, knowledge-base uploads, logs, `data/temp/`, and `data/output/` must stay local.
4. Do not commit local backup files such as `_backup_*.py`, `*_backup_*`, `*.bak`, or `*.py.bak`.
5. Treat changes under `data/config/` as sensitive. Commit only redacted templates or reviewed non-secret config.

## PR instructions

1. Title format: use conventional commit messages
2. Use English to write PR title and descriptions.

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

After any test or check command finishes, inspect its output immediately, resolve reported failures or hygiene issues, and rerun the relevant check before handing work off.

For knowledge-base import work, the first contract is:

```text
harness/contracts/knowledge_base_import.json
```
