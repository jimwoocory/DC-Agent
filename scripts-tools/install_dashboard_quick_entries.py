#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "astrbot" / "dashboard" / "quick_entries.py"


def _load_installer():
    spec = importlib.util.spec_from_file_location(
        "dc_dashboard_quick_entries", MODULE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load module: {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.install_dashboard_quick_entries


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install or check the DC dashboard quick-entry injection.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only check whether the quick-entry script is installed.",
    )
    parser.add_argument(
        "--dist",
        type=Path,
        default=REPO_ROOT / "data" / "dist",
        help="Dashboard dist directory to patch.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress status output.",
    )
    args = parser.parse_args()

    install_dashboard_quick_entries = _load_installer()
    result = install_dashboard_quick_entries(args.dist, check_only=args.check)
    if not args.quiet:
        for message in result.messages:
            print(message)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
