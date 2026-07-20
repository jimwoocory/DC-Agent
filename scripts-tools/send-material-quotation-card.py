#!/Users/dianchi/DC-Agent/.venv/bin/python
"""Send a live material-quotation workspace card to one Feishu receiver."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_local_environment() -> None:
    """Load local runtime variables without printing credential values."""
    env_path = Path.home() / ".dc-agent.env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() and key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip().strip("'\"")


load_local_environment()

import runtime_bootstrap  # noqa: E402

runtime_bootstrap.initialize_runtime_bootstrap()

from dc_engines.assistant_workbench_cards import (  # noqa: E402
    build_assistant_task_workspace_card,
)
from dc_engines.card_runtime import send_card_via_runtime  # noqa: E402
from dc_engines.feishu_card_streamer import FeishuCardStreamer  # noqa: E402
from dc_engines.feishu_hub import get_client, get_credentials  # noqa: E402

from astrbot.dashboard.api.assistant_attachments import draft_store  # noqa: E402


async def send_card(args: argparse.Namespace) -> int:
    """Create, send, and bind a live quotation workspace capability.

    Args:
        args: Parsed receiver, platform, and origin arguments.

    Returns:
        Zero when Feishu accepts the card, otherwise a non-zero status.
    """
    client = get_client()
    credentials = get_credentials()
    if client is None or credentials is None:
        print(json.dumps({"ok": False, "error": "feishu credentials unavailable"}))
        return 1

    origin = args.origin.strip().rstrip("/")
    draft = draft_store.create(
        platform_id=args.platform_id,
        upload_base_url=origin,
        task_type="quotation",
        session_id=args.receive_id,
        message_type="FriendMessage",
        sender_id=args.receive_id if args.receive_id_type == "open_id" else "",
    )
    card = build_assistant_task_workspace_card(
        task_type="quotation",
        app_id=credentials.app_id,
        workspace_url=draft.upload_url,
    )
    stream = await send_card_via_runtime(
        FeishuCardStreamer(client),
        card_type="assistant_task_intake",
        chat_id=args.receive_id,
        receive_id_type=args.receive_id_type,
        card=card,
        platform_id=args.platform_id,
        event="network_recovery",
        detail="standard-port company LAN quotation workspace",
    )
    if stream is None or not stream.message_id:
        print(json.dumps({"ok": False, "error": "card send failed"}))
        return 2
    draft_store.bind_message(draft.token, stream.message_id)
    print(
        json.dumps(
            {"ok": True, "message_id": stream.message_id, "origin": origin},
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    """Parse command-line arguments and send the live quotation card.

    Returns:
        Process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receive-id", required=True)
    parser.add_argument(
        "--receive-id-type",
        choices=["open_id", "user_id", "union_id", "email", "chat_id"],
        default="open_id",
    )
    parser.add_argument("--platform-id", default="巅池-Agent小助手")
    parser.add_argument(
        "--origin",
        default=os.environ.get("DC_ASSISTANT_H5_ORIGIN", "http://192.168.1.35:6185"),
    )
    return asyncio.run(send_card(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
