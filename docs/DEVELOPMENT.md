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
