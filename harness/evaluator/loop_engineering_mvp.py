"""Validate the Loop Engineering MVP harness contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from dc_engines.harness.loop_runtime import LOOP_EVENT_TYPES, LOOP_VERSION

from harness.evaluator.kb_import_contract import (
    REQUIRED_CRITERIA_KEYS,
    REQUIRED_TOP_LEVEL_KEYS,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT = REPO_ROOT / "harness" / "contracts" / "loop_engineering_mvp.json"
REQUIRED_PAYLOAD_KEYS = {
    "loop_version",
    "step_id",
    "kind",
    "summary",
    "status",
    "evidence",
    "metadata",
}


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
    else:
        seen_ids: set[str] = set()
        for index, criterion in enumerate(criteria, start=1):
            if not isinstance(criterion, dict):
                errors.append(f"criterion {index} must be an object")
                continue
            missing_keys = sorted(REQUIRED_CRITERIA_KEYS - criterion.keys())
            if missing_keys:
                errors.append(
                    f"criterion {index} missing keys: {', '.join(missing_keys)}"
                )
            criterion_id = criterion.get("id")
            if not isinstance(criterion_id, str) or not criterion_id:
                errors.append(f"criterion {index} id must be a non-empty string")
            elif criterion_id in seen_ids:
                errors.append(f"duplicate criterion id: {criterion_id}")
            else:
                seen_ids.add(criterion_id)
            verification = criterion.get("verification")
            if not isinstance(verification, str) or not verification.startswith(
                "uv run "
            ):
                errors.append(
                    f"criterion {index} verification must be a uv run command"
                )

    event_types = contract.get("loop_event_types")
    if event_types != list(LOOP_EVENT_TYPES):
        errors.append("loop_event_types must match dc_engines.harness.loop_runtime")

    payload_keys = contract.get("loop_payload_required_keys")
    if set(payload_keys or []) != REQUIRED_PAYLOAD_KEYS:
        errors.append("loop_payload_required_keys must match the MVP payload shape")

    if LOOP_VERSION != "mvp-1":
        errors.append("LOOP_VERSION must stay mvp-1 for this contract")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    args = parser.parse_args()

    contract = load_contract(args.contract)
    errors = validate_contract(contract)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"Contract valid: {args.contract}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
