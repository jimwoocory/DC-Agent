#!/usr/bin/env python3
"""Build a redacted router decision review pack from observations."""

from __future__ import annotations

import argparse
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

from dc_router.observation_capture import (  # noqa: E402
    DEFAULT_OBSERVATION_PATH,
    write_review_pack,
)

DEFAULT_OUTPUT_PATH = (
    ROOT
    / "harness"
    / "replay"
    / "proposals"
    / "router_decision_framework_review_pack.json"
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a redacted router review pack from observations."
    )
    parser.add_argument(
        "--observations",
        type=Path,
        default=DEFAULT_OBSERVATION_PATH,
        help="Observation JSONL path.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Review pack JSON output path.",
    )
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    output_path = write_review_pack(
        observations_path=args.observations,
        output_path=args.output,
        limit=args.limit,
    )
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
