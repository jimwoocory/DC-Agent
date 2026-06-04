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
            errors.append(f"criterion {index} verification must be a uv run command")

    return errors


def verification_commands(contract: dict[str, Any]) -> list[str]:
    return [
        criterion["verification"]
        for criterion in contract["acceptance_criteria"]
        if isinstance(criterion, dict)
        and isinstance(criterion.get("verification"), str)
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
