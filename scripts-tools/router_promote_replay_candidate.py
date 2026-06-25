#!/usr/bin/env python3
"""Promote an approved router replay candidate into the replay dataset."""

from __future__ import annotations

import argparse
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGINS_PARENT = ROOT / "data" / "plugins"
if str(PLUGINS_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGINS_PARENT))

dc_router_pkg = types.ModuleType("dc_router")
dc_router_pkg.__path__ = [str(PLUGINS_PARENT / "dc_router")]  # type: ignore[attr-defined]
dc_router_pkg.__file__ = str(PLUGINS_PARENT / "dc_router" / "__init__.py")  # type: ignore[attr-defined]
sys.modules.setdefault("dc_router", dc_router_pkg)

from dc_router.observation_capture import promote_replay_candidate  # noqa: E402

DEFAULT_REPLAY_PATH = ROOT / "harness" / "replay" / "router_decision_framework_v1.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Append an approved, human-reviewed router replay candidate."
    )
    parser.add_argument("candidate", type=Path, help="Approved replay candidate JSON.")
    parser.add_argument(
        "--replay",
        type=Path,
        default=DEFAULT_REPLAY_PATH,
        help="Replay dataset path.",
    )
    args = parser.parse_args()

    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    if not isinstance(candidate, dict):
        raise TypeError("Replay candidate file must contain a JSON object")
    replay_sample = promote_replay_candidate(candidate, replay_path=args.replay)
    print(replay_sample["sample_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
