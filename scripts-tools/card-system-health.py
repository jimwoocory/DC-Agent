#!/Users/dianchi/DC-Agent/.venv/bin/python
"""Run the Feishu card system engineering health check."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import runtime_bootstrap  # noqa: E402

runtime_bootstrap.initialize_runtime_bootstrap()

from dc_engines.card_system import (  # noqa: E402
    card_system_next_step,
    list_card_specs,
    list_card_versions,
    load_card_contract,
    recent_card_runtime_events,
    rollback_card_version,
    run_card_system_health,
    set_card_version,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Card Runtime health")
    parser.add_argument("--json", action="store_true", help="print machine JSON")
    parser.add_argument("--list", action="store_true", help="list registered cards")
    parser.add_argument("--versions", action="store_true", help="list card versions")
    parser.add_argument("--contract", action="store_true", help="print contract gates")
    parser.add_argument(
        "--events", action="store_true", help="show recent runtime events"
    )
    parser.add_argument("--next-step", action="store_true", help="print next action")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--set-version", nargs=2, metavar=("CARD_TYPE", "VERSION"))
    parser.add_argument("--rollback", metavar="CARD_TYPE", help="rollback one card")
    parser.add_argument(
        "--rollout", default="grey", choices=["stable", "grey", "rollback"]
    )
    parser.add_argument("--updated-by", default="operator")
    parser.add_argument("--note", default="")
    args = parser.parse_args()

    if args.set_version:
        card_type, version = args.set_version
        payload = set_card_version(
            card_type,
            version,
            rollout=args.rollout,
            updated_by=args.updated_by,
            note=args.note,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.rollback:
        payload = rollback_card_version(
            args.rollback,
            updated_by=args.updated_by,
            note=args.note,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.list:
        payload = {"cards": list_card_specs()}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for spec in payload["cards"]:
                print(
                    f"{spec['card_type']} v{spec['version']} "
                    f"owner={spec['owner']} builder={spec['builder']}"
                )
        return 0

    if args.versions:
        payload = {"versions": list_card_versions()}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for item in payload["versions"]:
                previous = item.get("previous_version") or "-"
                print(
                    f"{item['card_type']} active={item['active_version']} "
                    f"previous={previous} rollout={item.get('rollout', '-')}"
                )
        return 0

    if args.contract:
        payload = load_card_contract()
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"Contract: {payload.get('feature')}")
            for item in payload.get("acceptance_criteria", []):
                if not isinstance(item, dict):
                    continue
                print(f"- {item.get('id')}: {item.get('description')}")
                print(f"  verify: {item.get('verification')}")
        return 0

    if args.events:
        payload = {"events": recent_card_runtime_events(args.limit)}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for item in payload["events"]:
                status = "OK" if item.get("ok") else "FAILED"
                print(
                    f"{item.get('ts')} {status} {item.get('event')} "
                    f"{item.get('card_type')} message={item.get('message_id') or '-'} "
                    f"fallback={item.get('fallback') or '-'}"
                )
        return 0

    if args.next_step:
        report = run_card_system_health()
        if args.json:
            print(
                json.dumps(
                    {"ok": report.ok, "next_step": card_system_next_step(report)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(card_system_next_step(report))
        return 0 if report.ok else 2

    report = run_card_system_health()
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print("Card System:", "OK" if report.ok else "FAILED")
        for name, ok in report.checks.items():
            status = "OK" if ok else "FAILED"
            detail = report.details.get(name, "")
            suffix = f" - {detail}" if detail else ""
            print(f"- {name}: {status}{suffix}")
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
