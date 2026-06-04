#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for import_path in (ROOT, ROOT / "dc_engines"):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from dc_engines.assistant_distillation import (  # noqa: E402
    DEFAULT_DISTILLATION_DB_PATH,
    DEFAULT_LANGUAGE_OVERRIDES_PATH,
    AssistantDistillationStore,
    apply_candidate,
    approve_candidate,
    build_candidate_review_card,
    collect_distillation_metrics,
    generate_chitchat_candidates_from_miss_log,
    generate_intent_candidates_from_inbox,
    generate_tone_candidates_from_inbox,
    load_audit_events,
    reject_candidate,
    rollback_language_overrides,
)

DEFAULT_MISS_LOG_PATH = ROOT / "data" / "chitchat_guard_misses.jsonl"
DEFAULT_HIT_LOG_PATH = ROOT / "data" / "chitchat_guard_hits.jsonl"
DEFAULT_INBOX_DB_PATH = ROOT / "data" / "ai_inbox.db"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate, review, and apply DC-Agent assistant language candidates."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DISTILLATION_DB_PATH)
    parser.add_argument(
        "--allowed-reviewer",
        action="append",
        default=[],
        help="Allowed reviewer open_id/user id. Can be repeated.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate")
    generate.add_argument("--miss-log", type=Path, default=DEFAULT_MISS_LOG_PATH)
    generate.add_argument("--inbox-db", type=Path, default=DEFAULT_INBOX_DB_PATH)
    generate.add_argument("--min-count", type=int, default=3)

    list_cmd = subparsers.add_parser("list")
    list_cmd.add_argument("--status", default="pending")
    list_cmd.add_argument("--limit", type=int, default=50)

    audit = subparsers.add_parser("audit")
    audit.add_argument("--candidate-id", default="")
    audit.add_argument("--limit", type=int, default=100)

    metrics = subparsers.add_parser("metrics")
    metrics.add_argument("--miss-log", type=Path, default=DEFAULT_MISS_LOG_PATH)
    metrics.add_argument("--hit-log", type=Path, default=DEFAULT_HIT_LOG_PATH)

    card = subparsers.add_parser("card")
    card.add_argument("candidate_id")

    approve = subparsers.add_parser("approve")
    approve.add_argument("candidate_id")
    approve.add_argument("--reviewer", default="operator")

    reject = subparsers.add_parser("reject")
    reject.add_argument("candidate_id")
    reject.add_argument("--reviewer", default="operator")

    apply = subparsers.add_parser("apply")
    apply.add_argument("candidate_id")
    apply.add_argument("--reviewer", default="operator")
    apply.add_argument(
        "--overrides", type=Path, default=DEFAULT_LANGUAGE_OVERRIDES_PATH
    )

    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("rollback_path", type=Path)
    rollback.add_argument(
        "--overrides", type=Path, default=DEFAULT_LANGUAGE_OVERRIDES_PATH
    )

    args = parser.parse_args()
    store = AssistantDistillationStore(args.db)
    allowed_reviewers = set(args.allowed_reviewer) if args.allowed_reviewer else None

    if args.command == "generate":
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
        }
    elif args.command == "list":
        result = {
            "items": [
                {
                    "candidate_id": candidate.candidate_id,
                    "kind": candidate.kind,
                    "status": candidate.status,
                    "intent": candidate.intent,
                    "normalized_text": candidate.normalized_text,
                    "pattern": candidate.pattern,
                    "template_name": candidate.template_name,
                    "count": candidate.count,
                    "rationale": candidate.rationale,
                    "updated_at": candidate.updated_at,
                }
                for candidate in store.list_candidates(
                    status=args.status or None,
                    limit=args.limit,
                )
            ]
        }
    elif args.command == "audit":
        result = {
            "items": [
                asdict(event)
                for event in load_audit_events(
                    store,
                    candidate_id=args.candidate_id or None,
                    limit=args.limit,
                )
            ]
        }
    elif args.command == "metrics":
        result = collect_distillation_metrics(
            store=store,
            miss_log_path=args.miss_log,
            hit_log_path=args.hit_log,
        )
    elif args.command == "card":
        candidate = store.get_candidate(args.candidate_id)
        if candidate is None:
            raise SystemExit(f"candidate not found: {args.candidate_id}")
        result = build_candidate_review_card(candidate)
    elif args.command == "approve":
        result = {
            "candidate": asdict(
                approve_candidate(
                    store,
                    args.candidate_id,
                    reviewer=args.reviewer,
                    allowed_reviewers=allowed_reviewers,
                )
            )
        }
    elif args.command == "reject":
        result = {
            "candidate": asdict(
                reject_candidate(
                    store,
                    args.candidate_id,
                    reviewer=args.reviewer,
                    allowed_reviewers=allowed_reviewers,
                )
            )
        }
    elif args.command == "apply":
        result = {
            "candidate": asdict(
                apply_candidate(
                    store,
                    args.candidate_id,
                    overrides_path=args.overrides,
                    reviewer=args.reviewer,
                    allowed_reviewers=allowed_reviewers,
                )
            ),
            "overrides_path": str(args.overrides),
        }
    elif args.command == "rollback":
        rollback_language_overrides(args.overrides, args.rollback_path)
        result = {
            "status": "rolled_back",
            "overrides_path": str(args.overrides),
            "rollback_path": str(args.rollback_path),
        }
    else:
        raise AssertionError(f"unknown command: {args.command}")

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
