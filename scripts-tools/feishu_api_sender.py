"""Feishu message sender via Open Platform API (silent, no browser/client needed).

Replaces the old Playwright/Swift RPA approach. Uses the existing feishu_hub
infrastructure for authentication and the lark_oapi SDK.

Usage:
    python feishu_api_sender.py --text "消息内容"
    python feishu_api_sender.py --text "消息内容" --open-id ou_xxx --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_env_file() -> None:
    """Load secrets from ~/.dc-agent.env if present (same as cron scripts)."""
    env_path = Path.home() / ".dc-agent.env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()

# Default recipient: 蔡挺's open_id under the DC-Agent feishu app
DEFAULT_OPEN_ID = "ou_129defaa7d62fdb15ffc1eab436791d6"
LOG_FILE = ROOT / "data" / "feishu_api_sender.log"

logger = logging.getLogger("feishu_api_sender")


def _setup_logging(verbose: bool = False) -> None:
    """Configure file + optional console logging."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.setLevel(level)
    if verbose:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)


async def _send_once(open_id: str, text: str) -> dict:
    """Single send attempt via feishu_hub API.

    Returns dict with keys: ok, code, msg, message_id.
    """
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
    )

    from dc_engines.dc_engines.feishu_hub import get_client, is_enabled

    if not is_enabled():
        return {"ok": False, "error": "feishu_hub is not enabled (check credentials)"}

    client = get_client()
    if client is None:
        return {"ok": False, "error": "feishu_hub client is None"}

    body = (
        CreateMessageRequestBody.builder()
        .receive_id(open_id)
        .msg_type("text")
        .content(json.dumps({"text": text}, ensure_ascii=False))
        .build()
    )

    req = (
        CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(body)
        .build()
    )

    try:
        resp = await client.im.v1.message.acreate(req)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    ok = resp.success()
    message_id = ""
    if ok and getattr(resp, "data", None):
        message_id = getattr(resp.data, "message_id", "") or ""

    return {
        "ok": bool(ok),
        "code": getattr(resp, "code", None),
        "msg": getattr(resp, "msg", None),
        "message_id": message_id,
    }


async def send_message(
    text: str,
    open_id: str = DEFAULT_OPEN_ID,
    max_retries: int = 2,
    verbose: bool = False,
) -> dict:
    """Send a text message to a Feishu user via Open Platform API.

    Args:
        text: Message text to send.
        open_id: Recipient's Feishu open_id.
        max_retries: Number of retry attempts on failure.
        verbose: Enable verbose logging.

    Returns:
        Result dict: {ok, method, target, text, message_id, duration_ms, ...}
    """
    _setup_logging(verbose)
    t0 = time.time()

    logger.info("Sending message to %s: %s", open_id, text[:80])

    last_result: dict = {}
    for attempt in range(max_retries + 1):
        if attempt > 0:
            delay = 2**attempt
            logger.warning("Retry %d/%d after %ds", attempt, max_retries, delay)
            await asyncio.sleep(delay)

        last_result = await _send_once(open_id, text)
        if last_result.get("ok"):
            break
        logger.warning(
            "Attempt %d failed: code=%s msg=%s",
            attempt + 1,
            last_result.get("code"),
            last_result.get("msg") or last_result.get("error"),
        )

    duration_ms = int((time.time() - t0) * 1000)
    ok = last_result.get("ok", False)

    result = {
        "ok": ok,
        "method": "feishu-open-api",
        "target": open_id,
        "text": text,
        "message_id": last_result.get("message_id", ""),
        "duration_ms": duration_ms,
    }

    if not ok:
        result["error"] = last_result.get("msg") or last_result.get("error", "unknown")
        result["code"] = last_result.get("code")
        logger.error("Send failed: %s", result["error"])
    else:
        logger.info("Sent OK, message_id=%s, %dms", result["message_id"], duration_ms)

    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Send Feishu message via Open Platform API (silent, no UI)"
    )
    ap.add_argument("--text", required=True, help="Message text to send")
    ap.add_argument(
        "--open-id",
        default=DEFAULT_OPEN_ID,
        help=f"Recipient open_id (default: {DEFAULT_OPEN_ID})",
    )
    ap.add_argument(
        "--max-retries", type=int, default=2, help="Max retry attempts (default: 2)"
    )
    ap.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = ap.parse_args()

    result = asyncio.run(
        send_message(
            text=args.text,
            open_id=args.open_id,
            max_retries=args.max_retries,
            verbose=args.verbose,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
