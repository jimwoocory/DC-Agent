"""Handoff DeepSeek script-chat exports into governed review queues."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dc_engines.assistant_distillation import AssistantDistillationStore
from dc_engines.memory_governance.models import GovernedMemory
from dc_engines.memory_governance.obsidian_codec import (
    apply_note_path,
    render_governance_note,
)
from dc_engines.memory_governance.store import MemoryGovernanceStore

GOVERNANCE_INBOX_DIR = Path("40_MemoryGovernance") / "Inbox"
DEFAULT_DEPARTMENT = "执行部影视编导"
DEFAULT_OWNER = "执行部影视编导"
DEFAULT_CUTOFF = "2025-09-01T00:00:00+08:00"

_WORK_RE = re.compile(
    r"(五菱|缤果|星光|MINIEV|mini|菱骏|红标|扬光|宝骏|柳汽|东风|风行|"
    r"乘龙|菱智|视频|脚本|文案|分镜|拍摄|创意|大纲|混剪|纪录片|采访|"
    r"传播|选题|账号|甲方|发布|转发|封面)"
)
_PERSONAL_RE = re.compile(
    r"(八字|塔罗|占卜|卦|鼻窦炎|支气管|减肥|卡路里|电费|合同|报销|"
    r"超速|姓氏|恋爱|女生|小说|出差|充电器|骂人|歇后语)"
)
_SENSITIVE_RE = re.compile(
    r"(手机号|电话|身份证|密码|token|密钥|工资|薪资|银行卡|账号|住址|地址)",
    re.IGNORECASE,
)

_LIGHTWEIGHT_RE = re.compile(r"(轻量化|好实现|低成本|落地|减少纯口播|不要.*纯口播)")
_OFFICIAL_RE = re.compile(
    r"(官方账号|视频号|抖音号|发布平台|发布文案|转发文案|封面文案)"
)
_SAFETY_RE = re.compile(
    r"(安全导向|避免.*误以为|不能让消费者误以为|妨碍交通|车辆质量问题)"
)


@dataclass(slots=True)
class DeepSeekArchiveHandoffSummary:
    scanned_conversations: int = 0
    eligible_conversations: int = 0
    exported_memories: int = 0
    skipped_memories: int = 0
    assistant_candidates: int = 0
    memory_ids: list[str] = field(default_factory=list)
    note_paths: list[Path] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned_conversations": self.scanned_conversations,
            "eligible_conversations": self.eligible_conversations,
            "exported_memories": self.exported_memories,
            "skipped_memories": self.skipped_memories,
            "assistant_candidates": self.assistant_candidates,
            "memory_ids": self.memory_ids,
            "note_paths": [str(path) for path in self.note_paths],
        }


def export_deepseek_archive_handoff(
    *,
    archive_path: Path | str,
    distillation_store: AssistantDistillationStore,
    governance_store: MemoryGovernanceStore,
    vault_path: Path | str,
    department: str = DEFAULT_DEPARTMENT,
    owner: str = DEFAULT_OWNER,
    cutoff_inserted_at: str = DEFAULT_CUTOFF,
    min_pattern_count: int = 3,
    max_items_per_month: int = 24,
    now: str | None = None,
) -> DeepSeekArchiveHandoffSummary:
    """Export DeepSeek conversation exports into pending review queues.

    The handoff intentionally uses REQUEST/RESPONSE fragments only and excludes
    DeepSeek THINK fragments from governed memory evidence.
    """

    now = now or _now_iso()
    archive_path = Path(archive_path)
    vault_path = Path(vault_path)
    governance_store.initialize()
    summary = DeepSeekArchiveHandoffSummary()

    conversations = _load_conversations(archive_path)
    summary.scanned_conversations = len(conversations)
    eligible = [
        item
        for item in (_conversation_digest(c) for c in conversations)
        if item
        and item["inserted_at"] >= cutoff_inserted_at
        and _is_work_conversation(item)
    ]
    summary.eligible_conversations = len(eligible)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in eligible:
        grouped.setdefault(str(item["inserted_at"])[:7], []).append(item)

    inbox_dir = vault_path / GOVERNANCE_INBOX_DIR
    inbox_dir.mkdir(parents=True, exist_ok=True)
    for month, items in sorted(grouped.items()):
        memory = _memory_from_month(
            archive_path,
            month,
            items,
            department=department,
            owner=owner,
            max_items=max_items_per_month,
            now=now,
        )
        existing = governance_store.get_memory(memory.memory_id)
        if existing and existing.review_status != "need_review":
            summary.skipped_memories += 1
            continue
        note_path = inbox_dir / f"{memory.memory_id}.md"
        memory = apply_note_path(memory, str(note_path))
        note_path.write_text(render_governance_note(memory), encoding="utf-8")
        governance_store.upsert_memory(memory)
        governance_store.append_audit(
            memory.memory_id,
            "deepseek_archive_handoff_exported",
            "deepseek-archive-distillation",
            {
                "source_path": str(archive_path),
                "month": month,
                "conversation_count": len(items),
            },
            created_at=now,
        )
        summary.exported_memories += 1
        summary.memory_ids.append(memory.memory_id)
        summary.note_paths.append(note_path)

    summary.assistant_candidates = _generate_content_workflow_candidate(
        archive_path,
        eligible,
        store=distillation_store,
        min_pattern_count=min_pattern_count,
    )
    return summary


def _load_conversations(archive_path: Path) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(archive_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _conversation_digest(conversation: dict[str, Any]) -> dict[str, Any] | None:
    inserted_at = str(conversation.get("inserted_at") or "")
    if not inserted_at:
        return None
    requests: list[str] = []
    responses: list[str] = []
    for node in (conversation.get("mapping") or {}).values():
        message = (node or {}).get("message") or {}
        for fragment in message.get("fragments") or []:
            fragment_type = str(fragment.get("type") or "")
            content = str(fragment.get("content") or "").strip()
            if not content:
                continue
            if fragment_type == "REQUEST":
                requests.append(content)
            elif fragment_type == "RESPONSE":
                responses.append(content)
    if not requests:
        return None
    return {
        "id": str(conversation.get("id") or ""),
        "title": str(conversation.get("title") or "Untitled DeepSeek conversation"),
        "inserted_at": inserted_at,
        "updated_at": str(conversation.get("updated_at") or inserted_at),
        "requests": requests,
        "responses": responses,
    }


def _is_work_conversation(item: dict[str, Any]) -> bool:
    text = _conversation_text(item)
    return bool(_WORK_RE.search(text)) and not bool(_PERSONAL_RE.search(text))


def _memory_from_month(
    archive_path: Path,
    month: str,
    items: list[dict[str, Any]],
    *,
    department: str,
    owner: str,
    max_items: int,
    now: str,
) -> GovernedMemory:
    source_hash = _source_hash(archive_path, month, items)
    canonical_text = _canonical_month_digest(items, max_items=max_items)
    title = f"DeepSeek Archive Digest - {department}/{month}"
    return GovernedMemory(
        memory_id=_stable_deepseek_memory_id(str(archive_path), month, source_hash),
        source_system="conversation",
        source_id=f"deepseek_archive:{archive_path}#{month}",
        source_path=str(archive_path),
        source_hash=source_hash,
        title=title,
        summary=(
            f"{month} DeepSeek script-work archive for {department}: "
            f"{len(items)} eligible work conversations after filtering."
        ),
        canonical_text=canonical_text,
        memory_kind="process",
        review_status="need_review",
        confidence=0.6,
        sensitivity=_sensitivity_for_items(items),
        owner=owner,
        project_id="content_script_workflow",
        tags=[
            "deepseek-archive",
            "employee-memory",
            "content-sop",
            f"department:{department}",
            f"month:{month}",
        ],
        links=[str(archive_path)],
        obsidian_note_path="",
        governance_version=1,
        created_at=now,
        updated_at=now,
        approved_at="",
        approved_by="",
    )


def _canonical_month_digest(items: list[dict[str, Any]], *, max_items: int) -> str:
    lines = [
        "Filtered work conversations only. DeepSeek THINK fragments are intentionally omitted.",
    ]
    for item in items[:max_items]:
        requests = item.get("requests") or []
        responses = item.get("responses") or []
        first_request = _compact_text(str(requests[0] if requests else ""), limit=360)
        first_response = _compact_text(
            str(responses[0] if responses else ""), limit=220
        )
        lines.append(
            "\n".join(
                [
                    f"- {item['inserted_at']} | {item['title']}",
                    f"  - User request: {first_request}",
                    f"  - Assistant output preview: {first_response}",
                ]
            )
        )
    if len(items) > max_items:
        lines.append(f"- ... {len(items) - max_items} more conversations omitted")
    return "\n".join(lines)


def _generate_content_workflow_candidate(
    archive_path: Path,
    items: list[dict[str, Any]],
    *,
    store: AssistantDistillationStore,
    min_pattern_count: int,
) -> int:
    evidence: list[dict[str, Any]] = []
    matched_count = 0
    for item in items:
        text = _conversation_text(item)
        matched_patterns = [
            name
            for name, regex in (
                ("lightweight_shooting", _LIGHTWEIGHT_RE),
                ("official_channel_copy", _OFFICIAL_RE),
                ("safe_brand_framing", _SAFETY_RE),
            )
            if regex.search(text)
        ]
        if not matched_patterns:
            continue
        matched_count += 1
        evidence.append(
            {
                "source_path": str(archive_path),
                "conversation_id": item.get("id") or "",
                "timestamp": item.get("inserted_at") or "",
                "title": item.get("title") or "",
                "patterns": matched_patterns,
                "text": _compact_text(text, limit=240),
            }
        )
    if matched_count < min_pattern_count:
        return 0
    store.upsert_candidate(
        kind="tone_template",
        template_name="deepseek_archive_content_director_workflow",
        template_body=(
            "处理执行部影视编导的汽车短视频任务时，优先给可落地方案：明确竖屏/"
            "横屏、时长、镜号、画面、台词、道具和发布文案；减少纯口播，强调"
            "轻量化拍摄；涉及官方账号或汽车质量表述时，主动规避误导消费者和"
            "安全风险。"
        ),
        rationale=(
            "DeepSeek archive shows repeated content-director constraints around "
            "lightweight shooting, official channel copy, and safe brand framing."
        ),
        source="deepseek_archive_handoff",
        evidence=evidence[:10],
        count=matched_count,
    )
    return 1


def _conversation_text(item: dict[str, Any]) -> str:
    return " ".join(
        [
            str(item.get("title") or ""),
            " ".join(str(text) for text in item.get("requests") or []),
        ]
    )


def _source_hash(archive_path: Path, month: str, items: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        {"month": month, "items": items}, ensure_ascii=False, sort_keys=True
    )
    digest = hashlib.sha256(f"{archive_path}\n{payload}".encode()).hexdigest()
    return f"sha256:{digest}"


def _stable_deepseek_memory_id(source_path: str, month: str, source_hash: str) -> str:
    digest = hashlib.sha1(f"{source_path}:{month}:{source_hash}".encode()).hexdigest()
    return f"mem_deepseek_{digest[:12]}"


def _sensitivity_for_items(items: list[dict[str, Any]]) -> str:
    joined = "\n".join(_conversation_text(item) for item in items)
    return "confidential" if _SENSITIVE_RE.search(joined) else "internal"


def _compact_text(text: str, *, limit: int) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1]}…"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
