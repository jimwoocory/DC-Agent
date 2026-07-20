#!/usr/bin/env python3
"""Send deterministic reminders for open watchdog repair reviews."""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

DC_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = DC_ROOT / "data" / "watchdog" / "repair_state.json"
EVENTS_PATH = DC_ROOT / "data" / "watchdog" / "review_notifications.jsonl"
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:6185/"


def _load_repair_engine():
    """Load the repair engine from the operational scripts directory.

    Returns:
        Loaded repair engine module.

    Raises:
        RuntimeError: If the module cannot be loaded.
    """
    module_path = DC_ROOT / "scripts-watchdog" / "repair_engine.py"
    spec = importlib.util.spec_from_file_location("review_repair_engine", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load repair engine: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_notifications(
    *,
    state_path: Path,
    events_path: Path,
    dashboard_url: str,
    sender: Callable[..., Any] | None = None,
    engine: Any | None = None,
    now: int | None = None,
) -> dict[str, int]:
    """Send due review notifications and mark only successful stages.

    Args:
        state_path: Persistent repair state containing open reviews.
        events_path: Append-only notification audit log.
        dashboard_url: HTTP(S) Dashboard navigation target.
        sender: Injectable alert sender. Defaults to the shared alert channel.
        engine: Injectable repair engine module for tests.
        now: Optional Unix timestamp for deterministic tests.

    Returns:
        Counts for due, successfully sent, and failed notifications.
    """
    if not state_path.exists():
        return {"due": 0, "sent": 0, "failed": 0}
    timestamp = int(time.time()) if now is None else int(now)
    repair_engine = engine or _load_repair_engine()

    events_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = events_path.with_name("review_notifier.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"due": 0, "sent": 0, "failed": 0}

        try:
            candidates = repair_engine.collect_due_review_notifications(
                state_path=state_path,
                now=timestamp,
            )
        except ValueError:
            return {"due": 0, "sent": 0, "failed": 1}
        if not candidates:
            return {"due": 0, "sent": 0, "failed": 0}
        if sender is None:
            sys.path.insert(0, str(DC_ROOT / "dc_engines"))
            from dc_engines.alert_channel import send_alert

            sender = send_alert

        sent = 0
        failed = 0
        stage_titles = {
            "created": "DC-Agent 人工介入待审核",
            "pending_15m": "DC-Agent 人工介入 15 分钟未接单",
            "pending_60m": "DC-Agent 人工介入超过 60 分钟",
            "acknowledged_4h": "DC-Agent 人工介入处理中超过 4 小时",
        }
        for candidate in candidates:
            body = "\n".join(
                [
                    f"**Incident**: `{candidate['incident_id']}`",
                    f"**服务**: `{candidate['service']}`",
                    f"**审核状态**: {candidate['review_status']}",
                    f"**等待时间**: {candidate['age_seconds']} 秒",
                    f"**摘要**: {candidate['summary']}",
                    f"**原因**: {candidate['reason']}",
                    "",
                    "请在 Dashboard 的“人工介入队列”中生成并确认 Control Plan。",
                    "通知按钮只负责打开 Dashboard，不执行任何修复动作。",
                ]
            )
            try:
                result = sender(
                    title=stage_titles[candidate["stage"]],
                    body=body,
                    level=candidate["level"],
                    action_url=dashboard_url,
                    action_label="打开 Dashboard 审核",
                )
            except Exception:  # noqa: BLE001
                failed += 1
                continue
            if not getattr(result, "success", False):
                failed += 1
                continue
            try:
                changed = repair_engine.mark_review_notification(
                    state_path=state_path,
                    incident_id=candidate["incident_id"],
                    stage=candidate["stage"],
                    notified_at=timestamp,
                )
            except ValueError:
                failed += 1
                continue
            if not changed:
                continue
            sent += 1
            event = {
                "schema_version": 1,
                "ts_unix": timestamp,
                "incident_id": candidate["incident_id"],
                "service": candidate["service"],
                "stage": candidate["stage"],
                "level": candidate["level"],
                "status": "sent",
            }
            with events_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        return {"due": len(candidates), "sent": sent, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    """Run one review notification scan.

    Args:
        argv: Optional command-line arguments.

    Returns:
        Zero when all due notifications succeeded, otherwise one.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--events", type=Path, default=EVENTS_PATH)
    parser.add_argument(
        "--dashboard-url",
        default=os.environ.get("DC_AGENT_DASHBOARD_URL", DEFAULT_DASHBOARD_URL),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    engine = _load_repair_engine()
    if args.dry_run:
        candidates = (
            engine.collect_due_review_notifications(state_path=args.state)
            if args.state.exists()
            else []
        )
        print(json.dumps(candidates, ensure_ascii=False, indent=2))
        return 0
    result = run_notifications(
        state_path=args.state,
        events_path=args.events,
        dashboard_url=args.dashboard_url,
        engine=engine,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
