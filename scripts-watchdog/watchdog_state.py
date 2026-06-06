from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def mark_disabled_probe_state(
    state: dict[str, Any],
    key: str,
    *,
    reason: str,
    since: str,
) -> dict[str, Any]:
    previous = state.get(key)
    entry = dict(previous) if isinstance(previous, dict) else {}
    entry["status"] = "disabled"
    entry["disabled_reason"] = reason
    was_disabled = isinstance(previous, dict) and previous.get("status") == "disabled"
    if not was_disabled:
        entry["since"] = since
    elif not entry.get("since"):
        entry["since"] = since
    state[key] = entry
    return state


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Update DC-Agent watchdog state")
    subparsers = parser.add_subparsers(dest="command", required=True)
    disabled = subparsers.add_parser("mark-disabled")
    disabled.add_argument("state_file")
    disabled.add_argument("key")
    disabled.add_argument("reason")
    disabled.add_argument("since")
    args = parser.parse_args()

    if args.command == "mark-disabled":
        path = Path(args.state_file)
        state = _load_state(path)
        mark_disabled_probe_state(
            state,
            args.key,
            reason=args.reason,
            since=args.since,
        )
        _write_state(path, state)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
