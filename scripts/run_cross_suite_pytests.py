#!/usr/bin/env python3
"""Run DC-Engines and root pytest selections in isolated processes."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


def main(argv: Sequence[str] | None = None) -> int:
    """Run selected tests without importing both ``tests`` packages together.

    Args:
        argv: Optional command-line arguments for programmatic invocation.

    Returns:
        The first failing pytest exit code, or zero when every group passes.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--dc-engine", action="append", default=[])
    parser.add_argument("--root", action="append", default=[])
    args = parser.parse_args(argv)
    if not args.dc_engine and not args.root:
        parser.error("at least one --dc-engine or --root test selection is required")

    project_root = Path(__file__).resolve().parents[1]
    for selections in (args.dc_engine, args.root):
        if not selections:
            continue
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *selections, "-q"],
            cwd=project_root,
            check=False,
        )
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
