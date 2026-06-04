"""W3 / 2A-3 Case 归档到 NAS（G4 闭环）。

把 Case 的 deliverables 打包写到 NAS 命名空间下，watchdog 会自动同步进 KB。

NAS 路径约定：
  {nas_root}/dc-agent-cases/{YYYY-MM}/{case_id[:8]}_{slug(case.name)[:40]}/
    manifest.json     case 元信息 + deliverables 索引
    history.md        case_events 流水
    deliverables/     按 kind 分子目录的实际文件（v0 只写 inline path）

离线 / 出错：写到 ``data/case_archive_dlq.jsonl``，状态仍变 archived（业务侧"完事"）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .case_store import CaseStore
    from .contracts import Case


@dataclass(slots=True)
class ArchiveResult:
    """归档结果。"""

    case_id: str
    success: bool
    nas_path: str | None = None
    manifest_path: str | None = None
    deliverables_count: int = 0
    error: str | None = None
    fallback_dlq_path: str | None = None


_INVALID_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _slug(name: str, max_len: int = 40) -> str:
    """生成文件系统安全的 slug，保留中文。"""
    s = (name or "case").strip()
    s = _INVALID_FILENAME.sub("", s)
    s = re.sub(r"\s+", "-", s)
    return s[:max_len] or "case"


def _build_manifest(case: Case, tasks: list[dict], deliverables: list[dict]) -> dict:
    return {
        "case_id": case.case_id,
        "name": case.name,
        "client_name": case.client_name,
        "status": case.status,
        "platform_id": case.platform_id,
        "session_id": case.session_id,
        "version": case.version,
        "roles": dict(case.roles or {}),
        "created_at": case.created_at,
        "updated_at": case.updated_at,
        "archived_at": datetime.utcnow().isoformat() + "Z",
        "task_ids": list(case.task_ids or []),
        "tasks": tasks,
        "deliverables": deliverables,
        "history_md_path": "history.md",
    }


def _render_history_md(case: Case, events: list[dict]) -> str:
    """把 case_events 流水生成可读 markdown。"""
    lines = [
        f"# {case.name}",
        "",
        f"- case_id: `{case.case_id}`",
        f"- client: {case.client_name or '—'}",
        f"- status: {case.status}",
        f"- created: {case.created_at}",
        f"- archived: {datetime.utcnow().isoformat()}Z",
        "",
        "## 事件流水",
        "",
    ]
    for e in events:
        ts = e.get("created_at", "")
        etype = e.get("event_type", "")
        payload = e.get("payload", {})
        lines.append(
            f"- `{ts}` **{etype}** — `{json.dumps(payload, ensure_ascii=False)[:120]}`"
        )
    return "\n".join(lines)


async def archive_to_nas(
    case: Case,
    *,
    case_store: CaseStore,
    nas_root: Path,
    dlq_path: Path,
) -> ArchiveResult:
    """主入口。任何 NAS 不可达 / IO 错误 → 落 DLQ，返回 ArchiveResult(success=False)。"""
    try:
        # NAS 路径
        month = datetime.utcnow().strftime("%Y-%m")
        dir_name = f"{case.case_id[:8]}_{_slug(case.name)}"
        case_dir = nas_root / "dc-agent-cases" / month / dir_name

        # 拉 deliverables + tasks 摘要 + events（重用 store 的 get_case_context 等方法）
        view = await case_store.get_case_context(case.case_id)
        deliverables = (view or {}).get("deliverables", [])
        tasks = (view or {}).get("tasks", [])
        events = await case_store.list_events(case.case_id)
        events_dicts = [
            {
                "event_id": ev.event_id,
                "event_type": ev.event_type,
                "payload": ev.payload,
                "created_at": ev.created_at,
            }
            for ev in events
        ]

        # 试着真写 NAS
        case_dir.mkdir(parents=True, exist_ok=True)
        manifest = _build_manifest(case, tasks, deliverables)
        manifest_path = case_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        history_path = case_dir / "history.md"
        history_path.write_text(
            _render_history_md(case, events_dicts), encoding="utf-8"
        )

        return ArchiveResult(
            case_id=case.case_id,
            success=True,
            nas_path=str(case_dir),
            manifest_path=str(manifest_path),
            deliverables_count=len(deliverables),
        )
    except Exception as exc:
        # 写 DLQ
        dlq_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "case_id": case.case_id,
            "case_name": case.name,
            "error": f"{type(exc).__name__}: {exc}",
        }
        try:
            with open(dlq_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return ArchiveResult(
            case_id=case.case_id,
            success=False,
            error=str(exc),
            fallback_dlq_path=str(dlq_path),
        )
