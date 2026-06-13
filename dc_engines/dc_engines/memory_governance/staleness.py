"""Detect approved governed memories whose NAS source content has changed.

约定（与 obsidian_memory_governance 契约一致）：
- 只扫描 source_system="nas" 且 review_status="approved" 的记忆——其余来源
  （deepseek_archive / harness）没有可重算的源内容指纹。
- source_hash 与 nas_memory.db documents.sha256 不一致 → 标记 stale，
  重写治理笔记回 Inbox 等待人工重审，并落 append-only 审计。
- 源文档已从 NAS 索引消失的记忆不自动标 stale（可能只是索引滞后），
  仅在结果里报告为 missing_source，留给人判断。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .exporter import INBOX_DIR
from .obsidian_codec import apply_note_path, render_governance_note
from .store import MemoryGovernanceStore


@dataclass(slots=True)
class StaleScanResult:
    scanned_count: int = 0
    stale_count: int = 0
    missing_source_count: int = 0
    stale_memory_ids: list[str] = field(default_factory=list)
    missing_source_memory_ids: list[str] = field(default_factory=list)
    note_paths: list[Path] = field(default_factory=list)


def mark_stale_memories(
    *,
    store: MemoryGovernanceStore,
    nas_db_path: Path | str,
    vault_path: Path | str,
    now: str,
    actor: str = "stale-scan",
) -> StaleScanResult:
    """Mark approved NAS-backed memories stale when their source hash drifted."""

    store.initialize()
    nas_db_path = Path(nas_db_path)
    vault_path = Path(vault_path)
    inbox_dir = vault_path / INBOX_DIR
    result = StaleScanResult()

    current_hashes = _current_nas_hashes(nas_db_path)
    for memory in store.list_memories(status="approved", limit=10000):
        if memory.source_system != "nas":
            continue
        result.scanned_count += 1
        doc_key = memory.source_id.removeprefix("nas:")
        current = current_hashes.get(doc_key)
        if current is None:
            result.missing_source_count += 1
            result.missing_source_memory_ids.append(memory.memory_id)
            continue
        if current == memory.source_hash:
            continue

        before_hash = memory.source_hash
        memory.review_status = "stale"
        memory.updated_at = now
        existing_note = str(memory.obsidian_note_path or "").strip()
        if existing_note:
            note_path = Path(existing_note)
        else:
            inbox_dir.mkdir(parents=True, exist_ok=True)
            note_path = inbox_dir / f"{memory.memory_id}.md"
        memory = apply_note_path(memory, str(note_path))
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(render_governance_note(memory), encoding="utf-8")
        store.upsert_memory(memory)
        store.append_audit(
            memory.memory_id,
            "marked_stale",
            actor,
            {
                "reason": "nas_source_hash_changed",
                "previous_source_hash": before_hash,
                "current_source_hash": current,
                "note_path": str(note_path),
            },
            created_at=now,
        )
        result.stale_count += 1
        result.stale_memory_ids.append(memory.memory_id)
        result.note_paths.append(note_path)

    return result


def _current_nas_hashes(nas_db_path: Path) -> dict[str, str]:
    if not nas_db_path.exists():
        return {}
    with sqlite3.connect(nas_db_path) as conn:
        rows = conn.execute("SELECT doc_key, sha256 FROM documents").fetchall()
    hashes: dict[str, str] = {}
    for doc_key, raw_hash in rows:
        value = str(raw_hash or "")
        if not value:
            continue
        hashes[str(doc_key)] = (
            value if value.startswith("sha256:") else f"sha256:{value}"
        )
    return hashes
