#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for import_path in (ROOT, ROOT / "dc_engines"):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from dc_engines.assistant_distillation import (  # noqa: E402
    DEFAULT_DISTILLATION_DB_PATH,
    AssistantDistillationStore,
    collect_distillation_metrics,
    generate_chitchat_candidates_from_miss_log,
    generate_intent_candidates_from_inbox,
    generate_tone_candidates_from_inbox,
)

DEFAULT_MISS_LOG_PATH = ROOT / "data" / "chitchat_guard_misses.jsonl"
DEFAULT_HIT_LOG_PATH = ROOT / "data" / "chitchat_guard_hits.jsonl"
DEFAULT_INBOX_DB_PATH = ROOT / "data" / "ai_inbox.db"


def run_once(args: argparse.Namespace) -> dict:
    store = AssistantDistillationStore(args.db)
    result = {
        "chitchat": generate_chitchat_candidates_from_miss_log(
            miss_log_path=args.miss_log,
            store=store,
            min_count=args.min_count,
        ),
        "tone": generate_tone_candidates_from_inbox(
            inbox_db_path=args.inbox_db,
            store=store,
        ),
        "intent": generate_intent_candidates_from_inbox(
            inbox_db_path=args.inbox_db,
            store=store,
        ),
        "metrics": collect_distillation_metrics(
            store=store,
            miss_log_path=args.miss_log,
            hit_log_path=args.hit_log,
        ),
    }
    if args.metrics_out:
        args.metrics_out.parent.mkdir(parents=True, exist_ok=True)
        args.metrics_out.write_text(
            json.dumps(result["metrics"], ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Background worker for DC-Agent assistant distillation candidates."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DISTILLATION_DB_PATH)
    parser.add_argument("--miss-log", type=Path, default=DEFAULT_MISS_LOG_PATH)
    parser.add_argument("--hit-log", type=Path, default=DEFAULT_HIT_LOG_PATH)
    parser.add_argument("--inbox-db", type=Path, default=DEFAULT_INBOX_DB_PATH)
    parser.add_argument("--min-count", type=int, default=3)
    parser.add_argument("--interval-sec", type=int, default=3600)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--metrics-out",
        type=Path,
        default=ROOT / "data" / "assistant_distillation_metrics.json",
    )
    args = parser.parse_args()

    while True:
        result = run_once(args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if args.once:
            return
        time.sleep(max(60, args.interval_sec))


if __name__ == "__main__":
    main()
