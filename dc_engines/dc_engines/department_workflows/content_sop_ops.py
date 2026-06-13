from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .content_rule_overrides import (
    DEFAULT_RULE_OVERRIDES_PATH,
    ContentSopRuleOverrideError,
    load_content_sop_rule_overrides,
)
from .content_rule_proposals import (
    DEFAULT_RULE_PROPOSALS_DB_PATH,
    ContentSopRuleProposal,
    ContentSopRuleProposalStore,
)
from .quality_gate import build_content_sop_quality_policy

DC_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GOVERNED_MEMORY_DB_PATH = DC_ROOT / "data" / "governed_memory.db"
DEFAULT_OBSIDIAN_VAULT_PATH = DC_ROOT / "ObsidianVault"
DEFAULT_OPS_REPORT_DIR = DC_ROOT / "data" / "output" / "content_sop_ops"

ReminderSeverity = Literal["info", "warning", "critical"]


@dataclass(frozen=True, slots=True)
class ContentSopOpsReminder:
    reminder_id: str
    severity: ReminderSeverity
    title: str
    message: str
    action: str
    target_id: str = ""
    target_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reminder_id": self.reminder_id,
            "severity": self.severity,
            "title": self.title,
            "message": self.message,
            "action": self.action,
            "target_id": self.target_id,
            "target_type": self.target_type,
        }


def build_content_sop_ops_dashboard(
    *,
    proposal_store: ContentSopRuleProposalStore | None = None,
    overrides_path: Path | str | None = None,
    governed_memory_db_path: Path | str | None = None,
    quality_results: list[dict[str, Any]] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    store = proposal_store or ContentSopRuleProposalStore(
        DEFAULT_RULE_PROPOSALS_DB_PATH
    )
    override_path = (
        Path(overrides_path) if overrides_path else DEFAULT_RULE_OVERRIDES_PATH
    )
    memory_db = (
        Path(governed_memory_db_path)
        if governed_memory_db_path
        else DEFAULT_GOVERNED_MEMORY_DB_PATH
    )
    proposals = _all_proposals(store)
    override_status = _override_status(override_path)
    memory_status = _governed_memory_status(memory_db)
    quality = summarize_content_sop_quality(quality_results or [])
    return {
        "generated_at": now or _utcnow(),
        "proposals": {
            "total": len(proposals),
            "by_status": _count_by(proposals, "status"),
            "pending": [
                proposal.to_card_dict()
                for proposal in proposals
                if proposal.status == "pending"
            ][:20],
            "approved_not_applied": [
                proposal.to_card_dict()
                for proposal in proposals
                if proposal.status == "approved_for_runtime"
            ][:20],
        },
        "runtime_overrides": override_status,
        "memory_governance": memory_status,
        "quality": quality,
        "ops_sop": build_content_sop_operational_sop(
            proposal_statuses=_count_by(proposals, "status"),
            runtime_status=override_status,
            memory_status=memory_status,
            quality=quality,
        ),
    }


def build_content_sop_ops_reminders(
    dashboard: dict[str, Any],
) -> list[ContentSopOpsReminder]:
    reminders: list[ContentSopOpsReminder] = []
    proposals = dashboard.get("proposals") if isinstance(dashboard, dict) else {}
    proposals = proposals if isinstance(proposals, dict) else {}
    runtime = dashboard.get("runtime_overrides") if isinstance(dashboard, dict) else {}
    runtime = runtime if isinstance(runtime, dict) else {}
    memory = dashboard.get("memory_governance") if isinstance(dashboard, dict) else {}
    memory = memory if isinstance(memory, dict) else {}
    quality = dashboard.get("quality") if isinstance(dashboard, dict) else {}
    quality = quality if isinstance(quality, dict) else {}

    for proposal in proposals.get("pending") or []:
        if not isinstance(proposal, dict):
            continue
        proposal_id = str(proposal.get("proposal_id") or "")
        reminders.append(
            ContentSopOpsReminder(
                reminder_id=f"pending_rule:{proposal_id}",
                severity="warning",
                title="内容 SOP 规则候选待审批",
                message=str(proposal.get("rule_text") or ""),
                action="open_rule_review",
                target_type="rule_proposal",
                target_id=proposal_id,
            )
        )
    for proposal in proposals.get("approved_not_applied") or []:
        if not isinstance(proposal, dict):
            continue
        proposal_id = str(proposal.get("proposal_id") or "")
        reminders.append(
            ContentSopOpsReminder(
                reminder_id=f"approved_not_applied:{proposal_id}",
                severity="critical",
                title="已批准规则尚未二次确认应用",
                message=str(proposal.get("rule_text") or ""),
                action="confirm_apply_rule",
                target_type="rule_proposal",
                target_id=proposal_id,
            )
        )
    if runtime.get("config_ok") is False:
        status = str(runtime.get("status") or "unknown")
        reminders.append(
            ContentSopOpsReminder(
                reminder_id=f"runtime_overrides:{status}",
                severity="critical" if status == "invalid" else "warning",
                title="内容 SOP runtime override 未就绪",
                message=str(
                    runtime.get("error")
                    or runtime.get("source")
                    or "runtime override config is not confirmed"
                ),
                action="review_runtime_overrides",
                target_type="runtime_overrides",
                target_id=status,
            )
        )
    need_review = int((memory.get("by_status") or {}).get("need_review") or 0)
    if need_review:
        reminders.append(
            ContentSopOpsReminder(
                reminder_id="memory_need_review",
                severity="warning",
                title="Obsidian 记忆待审核",
                message=f"{need_review} 条内容 SOP 经验记忆等待治理审核。",
                action="open_memory_governance",
                target_type="memory_review_queue",
            )
        )
    blocked_count = int(quality.get("blocked_count") or 0)
    if blocked_count:
        reminders.append(
            ContentSopOpsReminder(
                reminder_id="quality_blocked",
                severity="critical",
                title="内容 SOP 质量 gate 阻塞",
                message=f"{blocked_count} 个交付结果被质量 gate 阻塞，请运营复盘资料和来源要求。",
                action="review_quality_gate",
                target_type="quality_gate",
            )
        )
    return reminders


def build_content_sop_ops_reminder_card(
    reminders: list[ContentSopOpsReminder],
) -> dict[str, Any]:
    if not reminders:
        content = "当前没有需要处理的内容 SOP 运营提醒。"
        template = "green"
    else:
        content = "\n".join(f"- **{item.title}**：{item.message}" for item in reminders)
        template = (
            "red"
            if any(item.severity == "critical" for item in reminders)
            else "yellow"
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": "内容 SOP 运营提醒"},
        },
        "elements": [{"tag": "markdown", "content": content}],
    }


def summarize_content_sop_quality(
    quality_results: list[dict[str, Any]],
) -> dict[str, Any]:
    if not quality_results:
        return {
            "sample_count": 0,
            "average_score": None,
            "blocked_count": 0,
            "review_required_count": 0,
            "passed_count": 0,
            "minimum_score": build_content_sop_quality_policy()["minimum_score"],
        }
    scores = [
        int(item.get("score") or 0)
        for item in quality_results
        if isinstance(item, dict)
    ]
    statuses = [
        str(item.get("status") or "")
        for item in quality_results
        if isinstance(item, dict)
    ]
    return {
        "sample_count": len(scores),
        "average_score": round(sum(scores) / len(scores), 2) if scores else None,
        "blocked_count": statuses.count("blocked"),
        "review_required_count": statuses.count("review_required"),
        "passed_count": statuses.count("passed"),
        "minimum_score": build_content_sop_quality_policy()["minimum_score"],
    }


def confirm_content_sop_production_config(
    config: dict[str, Any],
    *,
    proposal_store_path: Path | str | None = None,
    overrides_path: Path | str | None = None,
    governed_memory_db_path: Path | str | None = None,
    obsidian_vault_path: Path | str | None = None,
) -> dict[str, Any]:
    ops_cfg = config.get("content_sop_ops") if isinstance(config, dict) else {}
    ops_cfg = ops_cfg if isinstance(ops_cfg, dict) else {}
    plugin_cfg = config.get("content_sop_rule_review_plugin")
    plugin_cfg = plugin_cfg if isinstance(plugin_cfg, dict) else config
    admin_reviewers = (
        plugin_cfg.get("admin_reviewers") if isinstance(plugin_cfg, dict) else []
    )

    paths = {
        "proposal_store": Path(proposal_store_path or DEFAULT_RULE_PROPOSALS_DB_PATH),
        "runtime_overrides": Path(overrides_path or DEFAULT_RULE_OVERRIDES_PATH),
        "governed_memory": Path(
            governed_memory_db_path or DEFAULT_GOVERNED_MEMORY_DB_PATH
        ),
        "obsidian_vault": Path(obsidian_vault_path or DEFAULT_OBSIDIAN_VAULT_PATH),
    }
    checks = {
        "admin_reviewers_configured": isinstance(admin_reviewers, list)
        and any(str(item).strip() for item in admin_reviewers),
        "feishu_reminders_enabled": bool(ops_cfg.get("feishu_reminders_enabled")),
        "scheduled_import_export_enabled": bool(
            ops_cfg.get("scheduled_import_export_enabled")
        ),
        "audit_report_enabled": bool(ops_cfg.get("audit_report_enabled")),
        "quality_minimum_score_confirmed": "quality_minimum_score" in ops_cfg
        and int(ops_cfg.get("quality_minimum_score") or 0)
        >= build_content_sop_quality_policy()["minimum_score"],
        "proposal_store_parent_exists": paths["proposal_store"].parent.exists(),
        "runtime_overrides_parent_exists": paths["runtime_overrides"].parent.exists(),
        "governed_memory_parent_exists": paths["governed_memory"].parent.exists(),
        "obsidian_vault_exists": paths["obsidian_vault"].exists(),
        "proposal_store_exists": paths["proposal_store"].exists(),
        "runtime_overrides_config_exists": paths["runtime_overrides"].exists(),
        "governed_memory_store_exists": paths["governed_memory"].exists(),
    }
    missing = [key for key, ok in checks.items() if not ok]
    return {
        "ready": not missing,
        "checks": checks,
        "missing": missing,
        "paths": {key: str(value) for key, value in paths.items()},
    }


def build_content_sop_operational_sop(
    *,
    proposal_statuses: dict[str, int],
    runtime_status: dict[str, Any] | None = None,
    memory_status: dict[str, Any],
    quality: dict[str, Any],
) -> dict[str, Any]:
    daily: list[str] = [
        "查看内容 SOP 运营看板中的待审批规则、已批准未应用规则和质量 gate 阻塞项。",
        "优先处理已批准未应用的规则，必须通过飞书二次确认后进入 runtime override。",
    ]
    weekly: list[str] = [
        "导入 Obsidian 治理结果，检查 approved memory 是否形成稳定规则候选。",
        "导出审计报表，复盘阻塞原因、回滚记录和新规则命中情况。",
    ]
    escalation: list[str] = []
    runtime_status = runtime_status if isinstance(runtime_status, dict) else {}
    if runtime_status.get("config_ok") is False:
        escalation.append(
            "内容 SOP runtime override 未就绪，先修复配置再扩大运行时使用。"
        )
    if proposal_statuses.get("approved_for_runtime"):
        escalation.append("存在已批准未应用规则，提醒内容 SOP 管理员二次确认。")
    if int((memory_status.get("by_status") or {}).get("need_review") or 0):
        escalation.append("存在 need_review 记忆，提醒 owner 在 Obsidian 完成治理。")
    if int(quality.get("blocked_count") or 0):
        escalation.append(
            "存在质量阻塞，运营需要复盘必填资料、来源依据和交付物完整性。"
        )
    return {
        "daily": daily,
        "weekly": weekly,
        "escalation": escalation,
    }


def export_content_sop_ops_audit_report(
    *,
    dashboard: dict[str, Any],
    reminders: list[ContentSopOpsReminder],
    output_dir: Path | str | None = None,
    now: str | None = None,
) -> Path:
    timestamp = (now or _utcnow()).replace(":", "").replace("-", "")
    report_dir = Path(output_dir) if output_dir else DEFAULT_OPS_REPORT_DIR
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"content_sop_ops_audit_{timestamp}.json"
    report_path.write_text(
        json.dumps(
            {
                "generated_at": now or _utcnow(),
                "dashboard": dashboard,
                "reminders": [item.to_dict() for item in reminders],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return report_path


def run_scheduled_content_sop_ops(
    config: dict[str, Any],
    *,
    proposal_store: ContentSopRuleProposalStore | None = None,
    overrides_path: Path | str | None = None,
    governed_memory_db_path: Path | str | None = None,
    quality_results: list[dict[str, Any]] | None = None,
    output_dir: Path | str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    ops_cfg = config.get("content_sop_ops") if isinstance(config, dict) else {}
    ops_cfg = ops_cfg if isinstance(ops_cfg, dict) else {}
    dashboard = build_content_sop_ops_dashboard(
        proposal_store=proposal_store,
        overrides_path=overrides_path,
        governed_memory_db_path=governed_memory_db_path,
        quality_results=quality_results,
        now=now,
    )
    reminders = build_content_sop_ops_reminders(dashboard)
    jobs: list[dict[str, Any]] = [
        {
            "job": "governance_import_export",
            "status": "planned"
            if ops_cfg.get("scheduled_import_export_enabled")
            else "disabled",
        }
    ]
    report_path = ""
    if ops_cfg.get("audit_report_enabled"):
        report_path = str(
            export_content_sop_ops_audit_report(
                dashboard=dashboard,
                reminders=reminders,
                output_dir=output_dir,
                now=now,
            )
        )
        jobs.append({"job": "audit_report", "status": "exported", "path": report_path})
    else:
        jobs.append({"job": "audit_report", "status": "disabled"})
    return {
        "dashboard": dashboard,
        "reminders": [item.to_dict() for item in reminders],
        "jobs": jobs,
        "report_path": report_path,
    }


def _all_proposals(
    store: ContentSopRuleProposalStore,
) -> list[ContentSopRuleProposal]:
    proposals: list[ContentSopRuleProposal] = []
    for status in (
        "pending",
        "approved_for_runtime",
        "applied",
        "rejected",
        "rolled_back",
    ):
        proposals.extend(store.list_proposals(status=status, limit=100))
    return proposals


def _override_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "config_ok": False,
            "status": "missing",
            "source": str(path),
            "error": "runtime override config missing",
            "version": None,
            "enabled_count": 0,
            "disabled_count": 0,
            "recent_audit": [],
        }
    try:
        config = load_content_sop_rule_overrides(path)
    except ContentSopRuleOverrideError as exc:
        return {
            "config_ok": False,
            "status": "invalid",
            "source": str(path),
            "error": str(exc),
            "version": None,
            "enabled_count": 0,
            "disabled_count": 0,
            "recent_audit": [],
        }
    rules = [rule for rule in config.get("rules") or [] if isinstance(rule, dict)]
    enabled_count = sum(1 for rule in rules if rule.get("enabled") is True)
    disabled_count = sum(1 for rule in rules if rule.get("enabled") is False)
    return {
        "config_ok": True,
        "status": "active" if enabled_count else "configured_empty",
        "source": str(path),
        "error": "",
        "version": config.get("version"),
        "enabled_count": enabled_count,
        "disabled_count": disabled_count,
        "recent_audit": list(config.get("audit") or [])[-20:],
    }


def _governed_memory_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"store_exists": False, "total": 0, "by_status": {}, "recent": []}
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT memory_id, title, review_status, updated_at
            FROM governed_memories
            ORDER BY updated_at DESC, memory_id
            LIMIT 20
            """
        ).fetchall()
        by_status = {
            str(row["key"]): int(row["count"])
            for row in conn.execute(
                """
                SELECT review_status AS key, COUNT(*) AS count
                FROM governed_memories
                GROUP BY review_status
                """
            ).fetchall()
        }
        total = conn.execute("SELECT COUNT(*) FROM governed_memories").fetchone()[0]
    return {
        "store_exists": True,
        "total": int(total),
        "by_status": by_status,
        "recent": [{key: row[key] for key in row.keys()} for row in rows],
    }


def _count_by(items: list[ContentSopRuleProposal], attr: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = str(getattr(item, attr))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
