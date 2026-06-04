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
