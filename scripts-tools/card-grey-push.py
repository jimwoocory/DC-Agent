#!/Users/dianchi/DC-Agent/.venv/bin/python
"""Send registered Feishu card samples to one receiver for grey validation."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import runtime_bootstrap  # noqa: E402

runtime_bootstrap.initialize_runtime_bootstrap()

from dc_engines.card_runtime import send_card_via_runtime  # noqa: E402
from dc_engines.card_system import (  # noqa: E402
    build_sample_card,
    list_card_specs,
)
from dc_engines.feishu_card_streamer import FeishuCardStreamer  # noqa: E402
from dc_engines.feishu_hub import get_client, is_enabled  # noqa: E402


def _card_types_from_args(args: argparse.Namespace) -> list[str]:
    if args.all:
        return [item["card_type"] for item in list_card_specs()]
    if args.card_type:
        return args.card_type
    raise SystemExit("provide --card-type or --all")


async def _send_cards(args: argparse.Namespace) -> int:
    if not is_enabled():
        print(
            "Feishu hub disabled. Load local credentials before grey push.",
            file=sys.stderr,
        )
        return 1

    client = get_client()
    streamer = FeishuCardStreamer(client)
    card_types = _card_types_from_args(args)
    results: list[dict[str, str | bool]] = []

    for card_type in card_types:
        try:
            card = build_sample_card(card_type)
            stream = await send_card_via_runtime(
                streamer,
                card_type=card_type,
                chat_id=args.receive_id,
                receive_id_type=args.receive_id_type,
                card=card,
                platform_id=args.platform_id,
                event="grey_push",
                detail=args.note or "grey validation push",
            )
            ok = stream is not None
            message_id = stream.message_id if stream else ""
            results.append({"card_type": card_type, "ok": ok, "message_id": message_id})
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "card_type": card_type,
                    "ok": False,
                    "message_id": "",
                    "error": str(exc),
                }
            )

    print(json.dumps({"sent": results}, ensure_ascii=False, indent=2))
    return 0 if all(item["ok"] for item in results) else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Push registered card samples to Feishu"
    )
    parser.add_argument(
        "--receive-id-type",
        required=True,
        choices=["email", "union_id", "user_id", "open_id", "chat_id"],
    )
    parser.add_argument("--receive-id", required=True)
    parser.add_argument("--card-type", action="append")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--platform-id", default="巅池-Agent小助手")
    parser.add_argument("--note", default="")
    args = parser.parse_args()
    return asyncio.run(_send_cards(args))


if __name__ == "__main__":
    raise SystemExit(main())
