#!/usr/bin/env python3
"""DEPRECATED: Use feishu_api_sender.py instead (Open Platform API, silent, no UI).

Legacy Python wrapper for the Swift-based Feishu Native RPA sender.
Calls the compiled FeishuRPA binary and provides retry logic, logging,
and a JSON interface compatible with the legacy Playwright-based sender.

Usage:
    python feishu_native_rpa_sender.py --text "消息内容" [--target "巅池-Agent小助手"] [--verbose]
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BINARY_PATH = SCRIPT_DIR / "feishu_native_rpa" / "bin" / "FeishuRPA"
LOG_FILE = SCRIPT_DIR.parent / "data" / "feishu_native_rpa.log"
DEFAULT_TARGET = "巅池-Agent小助手"

logger = logging.getLogger("feishu_native_rpa")


def _setup_logging(verbose: bool = False) -> None:
    """Configure logging to both stderr and a rotating log file."""
    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)

    # Stderr handler
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(level)
    stderr_handler.setFormatter(
        logging.Formatter("[%(name)s] %(levelname)s %(message)s")
    )
    logger.addHandler(stderr_handler)

    # File handler
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
        )
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("Cannot write to log file %s: %s", LOG_FILE, exc)


def find_binary() -> Path:
    """Locate the compiled FeishuRPA binary."""
    if not BINARY_PATH.exists():
        raise FileNotFoundError(
            f"FeishuRPA binary not found at {BINARY_PATH}. "
            "Run 'cd scripts-tools/feishu_native_rpa && make release' first."
        )
    return BINARY_PATH


def send_message(
    text: str,
    target: str = DEFAULT_TARGET,
    screenshot_dir: str = "/tmp",
    verbose: bool = False,
    dry_run: bool = False,
) -> dict:
    """Send a message via the Swift native RPA binary.

    Returns the JSON result dict from the binary's stdout.
    """
    binary = find_binary()
    cmd = [str(binary), "--text", text, "--target", target]

    if screenshot_dir != "/tmp":
        cmd.extend(["--screenshot-dir", screenshot_dir])
    if verbose:
        cmd.append("--verbose")
    if dry_run:
        cmd.append("--dry-run")

    logger.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "method": "rpa-native-accessibility",
            "target": target,
            "text": text,
            "error": "Swift binary timed out after 60s",
        }

    # Log stderr (Swift's verbose output)
    if result.stderr:
        for line in result.stderr.strip().splitlines():
            logger.debug("  %s", line)

    # Parse JSON stdout
    stdout = result.stdout.strip()
    if not stdout:
        return {
            "ok": False,
            "method": "rpa-native-accessibility",
            "target": target,
            "text": text,
            "error": f"No JSON output from binary (exit code {result.returncode})",
        }

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "method": "rpa-native-accessibility",
            "target": target,
            "text": text,
            "error": f"Invalid JSON from binary: {stdout[:200]}",
        }

    return data


def send_with_retry(
    text: str,
    target: str = DEFAULT_TARGET,
    max_retries: int = 2,
    **kwargs,
) -> dict:
    """Send a message with exponential backoff retry.

    Retries on failure with 3s, 6s delays between attempts.
    """
    last_result: dict = {}

    for attempt in range(max_retries + 1):
        if attempt > 0:
            delay = 3 * (2 ** (attempt - 1))
            logger.info("Retry %d/%d after %ds delay...", attempt, max_retries, delay)
            time.sleep(delay)

        last_result = send_message(text, target, **kwargs)

        if last_result.get("ok"):
            if attempt > 0:
                logger.info("Succeeded on attempt %d", attempt + 1)
            return last_result

        logger.warning(
            "Attempt %d failed: %s",
            attempt + 1,
            last_result.get("error", "unknown error"),
        )

    logger.error("All %d attempts failed", max_retries + 1)
    return last_result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Feishu Native RPA Sender (Swift Accessibility API)"
    )
    parser.add_argument("--text", required=True, help="Message text to send")
    parser.add_argument("--target", default=DEFAULT_TARGET, help="Target chat name")
    parser.add_argument(
        "--screenshot-dir", default="/tmp", help="Screenshot output directory"
    )
    parser.add_argument("--max-retries", type=int, default=2, help="Max retry attempts")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    parser.add_argument("--dry-run", action="store_true", help="Dry run (no send)")

    args = parser.parse_args()
    _setup_logging(args.verbose)

    result = send_with_retry(
        text=args.text,
        target=args.target,
        screenshot_dir=args.screenshot_dir,
        verbose=args.verbose,
        dry_run=args.dry_run,
        max_retries=args.max_retries if not args.dry_run else 0,
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
