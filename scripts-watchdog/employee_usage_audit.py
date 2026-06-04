#!/usr/bin/env python3
"""Aggregate employee usage signals for DC-Agent self-inspection."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DC_ROOT = Path("/Users/dianchi/DC-Agent")
DATA_DB = DC_ROOT / "data" / "data_v4.db"
OUT_DIR = DC_ROOT / "data" / "watchdog" / "usage_audit"
LATEST_PATH = OUT_DIR / "latest.json"
HISTORY_PATH = OUT_DIR / "history.jsonl"
VENV_PYTHON = DC_ROOT / ".venv" / "bin" / "python"


@dataclass(slots=True)
class UsageAudit:
    generated_at: str
    window_days: int
    provider_calls: int
    completed_calls: int
    error_calls: int
    error_rate: float
    avg_latency_sec: float | None
    p95_latency_sec: float | None
    total_input_tokens: int
    total_output_tokens: int
    active_users: int
    conversations: int
    platform_messages: int
    platform_counts: list[dict[str, Any]]
    provider_counts: list[dict[str, Any]]
    status_counts: list[dict[str, Any]]
    slow_calls: list[dict[str, Any]]
    recommendations: list[dict[str, str]]


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def rows(
    conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]
) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def scalar(
    conn: sqlite3.Connection, sql: str, params: tuple[Any, ...], default: Any = 0
) -> Any:
    value = conn.execute(sql, params).fetchone()[0]
    return default if value is None else value


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, round((len(values) - 1) * pct))
    return round(values[idx], 2)


def build_recommendations(
    audit: UsageAudit, previous_calls: int
) -> list[dict[str, str]]:
    recs: list[dict[str, str]] = []
    if audit.provider_calls == 0:
        recs.append(
            {
                "priority": "P1",
                "area": "adoption",
                "action": "近窗口没有模型调用，确认员工入口、飞书事件回调和 onboarding 是否正常。",
            }
        )
        return recs

    if audit.error_rate >= 0.15:
        recs.append(
            {
                "priority": "P0",
                "area": "reliability",
                "action": "模型调用错误率偏高，优先检查 provider 可用性、额度、超时和 fallback 路由。",
            }
        )
    elif audit.error_rate >= 0.05:
        recs.append(
            {
                "priority": "P1",
                "area": "reliability",
                "action": "模型调用已有明显错误，建议抽查 provider_stats 错误记录并补充告警阈值。",
            }
        )

    if audit.p95_latency_sec is not None and audit.p95_latency_sec >= 90:
        recs.append(
            {
                "priority": "P1",
                "area": "latency",
                "action": "P95 响应耗时过长，建议把高频轻任务切到更快模型，并限制超大上下文。",
            }
        )

    if (
        audit.provider_calls
        and audit.total_input_tokens / audit.provider_calls >= 30000
    ):
        recs.append(
            {
                "priority": "P1",
                "area": "cost",
                "action": "单次平均输入 token 偏高，建议检查知识库召回、长上下文压缩和重复历史注入。",
            }
        )

    if previous_calls > 0 and audit.provider_calls <= previous_calls * 0.35:
        recs.append(
            {
                "priority": "P2",
                "area": "adoption",
                "action": "使用量较上一周期明显下降，建议确认员工入口是否被覆盖、群内触发词是否变化。",
            }
        )

    if audit.active_users <= 1 and audit.provider_calls >= 5:
        recs.append(
            {
                "priority": "P2",
                "area": "adoption",
                "action": "使用集中在少数用户，建议补一次员工引导或在常用群固定入口说明。",
            }
        )

    if not recs:
        recs.append(
            {
                "priority": "P2",
                "area": "observe",
                "action": "当前窗口没有明显异常，继续积累真实使用样本后再做路由和知识库优化。",
            }
        )
    return recs


def collect(db_path: Path, days: int) -> UsageAudit:
    now = datetime.now(UTC)
    cutoff = (now - timedelta(days=days)).replace(tzinfo=None).isoformat(sep=" ")
    previous_cutoff = (
        (now - timedelta(days=days * 2)).replace(tzinfo=None).isoformat(sep=" ")
    )

    with connect(db_path) as conn:
        provider_calls = int(
            scalar(
                conn,
                "select count(*) from provider_stats where created_at >= ?",
                (cutoff,),
            )
        )
        completed_calls = int(
            scalar(
                conn,
                "select count(*) from provider_stats where created_at >= ? and status = 'completed'",
                (cutoff,),
            )
        )
        error_calls = int(
            scalar(
                conn,
                "select count(*) from provider_stats where created_at >= ? and status != 'completed'",
                (cutoff,),
            )
        )
        previous_calls = int(
            scalar(
                conn,
                "select count(*) from provider_stats where created_at >= ? and created_at < ?",
                (previous_cutoff, cutoff),
            )
        )
        token_row = conn.execute(
            """
            select
              coalesce(sum(token_input_other + token_input_cached), 0) as input_tokens,
              coalesce(sum(token_output), 0) as output_tokens
            from provider_stats
            where created_at >= ?
            """,
            (cutoff,),
        ).fetchone()
        latencies = [
            float(row[0])
            for row in conn.execute(
                """
                select max(0, end_time - start_time)
                from provider_stats
                where created_at >= ? and end_time > 0 and start_time > 0
                """,
                (cutoff,),
            ).fetchall()
            if row[0] is not None
        ]
        active_users = int(
            scalar(
                conn,
                "select count(distinct user_id) from conversations where created_at >= ?",
                (cutoff,),
            )
        )
        conversations = int(
            scalar(
                conn,
                "select count(*) from conversations where created_at >= ?",
                (cutoff,),
            )
        )
        platform_messages = int(
            scalar(
                conn,
                "select count(*) from platform_message_history where created_at >= ?",
                (cutoff,),
            )
        )
        platform_counts = rows(
            conn,
            """
            select platform_id, platform_type, sum(count) as count
            from platform_stats
            where timestamp >= ?
            group by platform_id, platform_type
            order by count desc
            limit 10
            """,
            (cutoff,),
        )
        provider_counts = rows(
            conn,
            """
            select provider_id, coalesce(provider_model, '') as provider_model, count(*) as count
            from provider_stats
            where created_at >= ?
            group by provider_id, provider_model
            order by count desc
            limit 10
            """,
            (cutoff,),
        )
        status_counts = rows(
            conn,
            """
            select status, count(*) as count
            from provider_stats
            where created_at >= ?
            group by status
            order by count desc
            """,
            (cutoff,),
        )
        slow_calls = rows(
            conn,
            """
            select
              id,
              created_at,
              status,
              provider_id,
              coalesce(provider_model, '') as provider_model,
              round(max(0, end_time - start_time), 2) as latency_sec,
              token_input_other + token_input_cached as input_tokens,
              token_output as output_tokens
            from provider_stats
            where created_at >= ?
            order by max(0, end_time - start_time) desc
            limit 5
            """,
            (cutoff,),
        )

    avg_latency = round(sum(latencies) / len(latencies), 2) if latencies else None
    audit = UsageAudit(
        generated_at=utc_now_iso(),
        window_days=days,
        provider_calls=provider_calls,
        completed_calls=completed_calls,
        error_calls=error_calls,
        error_rate=round(error_calls / provider_calls, 4) if provider_calls else 0.0,
        avg_latency_sec=avg_latency,
        p95_latency_sec=percentile(latencies, 0.95),
        total_input_tokens=int(token_row["input_tokens"]),
        total_output_tokens=int(token_row["output_tokens"]),
        active_users=active_users,
        conversations=conversations,
        platform_messages=platform_messages,
        platform_counts=platform_counts,
        provider_counts=provider_counts,
        status_counts=status_counts,
        slow_calls=slow_calls,
        recommendations=[],
    )
    audit.recommendations = build_recommendations(audit, previous_calls)
    return audit


def render_markdown(audit: UsageAudit) -> str:
    status = "正常"
    if audit.error_rate >= 0.15:
        status = "需要立即关注"
    elif audit.error_rate >= 0.05 or (
        audit.p95_latency_sec is not None and audit.p95_latency_sec >= 90
    ):
        status = "需要观察"

    provider_lines = [
        f"- {item['provider_id']} / {item['provider_model'] or '-'}: {item['count']} 次"
        for item in audit.provider_counts[:5]
    ] or ["- 暂无"]
    rec_lines = [
        f"- {item['priority']} · {item['area']}: {item['action']}"
        for item in audit.recommendations
    ]
    slow_lines = [
        f"- #{item['id']} {item['status']} {item['latency_sec']}s "
        f"{item['provider_model'] or item['provider_id']}"
        for item in audit.slow_calls[:3]
    ] or ["- 暂无"]

    return "\n".join(
        [
            f"## 员工使用自查 · {status}",
            "",
            f"窗口：最近 {audit.window_days} 天；生成时间：{audit.generated_at}",
            "",
            "### 核心指标",
            f"- 模型调用：{audit.provider_calls} 次，成功 {audit.completed_calls} 次，错误 {audit.error_calls} 次，错误率 {audit.error_rate:.1%}",
            f"- 平均耗时：{audit.avg_latency_sec if audit.avg_latency_sec is not None else '-'}s；P95：{audit.p95_latency_sec if audit.p95_latency_sec is not None else '-'}s",
            f"- Token：输入 {audit.total_input_tokens}，输出 {audit.total_output_tokens}",
            f"- 活跃用户：{audit.active_users}；会话：{audit.conversations}；平台消息入库：{audit.platform_messages}",
            "",
            "### Provider 分布",
            *provider_lines,
            "",
            "### 最慢调用",
            *slow_lines,
            "",
            "### 建议",
            *rec_lines,
        ]
    )


def write_outputs(audit: UsageAudit) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = asdict(audit)
    LATEST_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with HISTORY_PATH.open("a", encoding="utf-8") as history:
        history.write(json.dumps(payload, ensure_ascii=False) + "\n")


def send_alert(markdown: str, level: str) -> int:
    python = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
    result = subprocess.run(
        [
            str(python),
            "-m",
            "dc_engines.alert_channel",
            "--title",
            "DC-Agent 员工使用自查",
            "--body",
            markdown,
            "--level",
            level,
            "--quiet",
        ],
        cwd=str(DC_ROOT),
        check=False,
    )
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="DC-Agent employee usage audit")
    parser.add_argument("--db", type=Path, default=DATA_DB)
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--send", action="store_true", help="send via alert_channel")
    parser.add_argument(
        "--level", choices=["info", "warning", "critical"], default="info"
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 2
    if args.days <= 0:
        print("--days must be positive", file=sys.stderr)
        return 2

    audit = collect(args.db, args.days)
    write_outputs(audit)
    markdown = render_markdown(audit)
    print(markdown)
    if args.send:
        return send_alert(markdown, args.level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
