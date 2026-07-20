#!/usr/bin/env python3
"""Run Feishu business MVP sync, notification, and report workflows."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

DC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DC_ROOT / "dc_engines"))

from dc_engines.feishu_business_mvp import (  # noqa: E402
    build_default_runner,
    load_business_mvp_config,
    load_business_mvp_config_payload,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments.

    Args:
        argv: Optional argument list.

    Returns:
        Parsed argparse namespace.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "actions",
        nargs="*",
        choices=[
            "preflight",
            "sync-assets",
            "sync-onboarding",
            "sync-finance",
            "notify-exceptions",
            "weekly-report",
        ],
        help="Workflow actions to run. Defaults to preflight.",
    )
    parser.add_argument(
        "--config",
        default=str(DC_ROOT / "data" / "config" / "feishu_business_mvp.json"),
        help="Business config JSON path.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    return parser.parse_args(argv)


async def run(argv: list[str] | None = None) -> dict[str, Any]:
    """Run selected workflows and return JSON-friendly output.

    Args:
        argv: Optional argument list.

    Returns:
        Workflow result.
    """

    args = parse_args(argv)
    config = load_business_mvp_config(args.config)
    raw_config = load_business_mvp_config_payload(args.config)
    if not Path(config.db_path).is_absolute():
        config.db_path = DC_ROOT / config.db_path
    runner = build_default_runner(config)
    return await runner.run_once(list(args.actions), raw_config=raw_config)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint.

    Args:
        argv: Optional argument list.

    Returns:
        Process exit code.
    """

    args = parse_args(argv)
    result = asyncio.run(run(argv))
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
