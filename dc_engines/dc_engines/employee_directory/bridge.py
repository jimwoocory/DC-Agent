"""Bridge employee memory with runtime KB context and controlled archives."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import Employee, EmployeeMemory, MemoryKind

EXPORTABLE_MEMORY_KINDS: set[MemoryKind] = {
    "preference",
    "skill",
    "role",
    "persona",
}
DEFAULT_KB_NAMES: tuple[str, ...] = ("中台运营", "nas_knowledge")


@dataclass(slots=True)
class EmployeeMemoryArchiveResult:
    status: str
    target: str = ""
    message: str = ""


@dataclass(slots=True)
class EmployeeMemoryBridge:
    kb_names: tuple[str, ...] = DEFAULT_KB_NAMES
    kb_context_char_limit: int = 1200
    nas_inbox_dir: Path | None = None
    exportable_kinds: set[MemoryKind] = field(
        default_factory=lambda: set(EXPORTABLE_MEMORY_KINDS)
    )

    def build_kb_query(
        self,
        emp: Employee,
        user_text: str,
        memories: list[EmployeeMemory],
        *,
        preferred_address: str = "",
    ) -> str:
        """Build a privacy-light KB query from the employee context and request."""
        parts = [
            user_text.strip(),
            emp.department.strip(),
            emp.role.strip(),
            preferred_address.strip(),
            emp.communication_style.strip(),
        ]
        for memory in memories[:4]:
            if memory.kind in self.exportable_kinds:
                parts.append(self._strip_source_note(memory.content))
        query = " ".join(part for part in parts if part)
        return re.sub(r"\s+", " ", query).strip()[:500]

    def render_kb_context_block(self, context_text: str) -> str:
        text = (context_text or "").strip()
        if not text:
            return ""
        return (
            "## 公司知识库补充（员工记忆桥接，只读检索）\n"
            "- 以下内容来自当前会话已绑定的 AstrBot 知识库/NAS 摄入知识库；"
            "优先用于补充业务事实，不覆盖上面的身份与称呼铁律。\n"
            f"{text[: self.kb_context_char_limit]}"
        )

    def build_archive_document(
        self,
        emp: Employee,
        memories: list[EmployeeMemory],
        *,
        preferred_address: str,
        relation_type: str,
    ) -> str:
        """Build a sanitized internal profile document for KB/NAS archival."""
        now = datetime.now(timezone.utc).isoformat()
        name = emp.display_name or preferred_address or "未命名员工"
        lines = [
            f"# 员工协作画像：{name}",
            "",
            "> 来源：DC-Agent employee_memory_identity。本文档只归档协作偏好、称呼策略和工作画像；不包含 open_id、原始私聊全文或未筛选事实。",
            "",
            f"- 生成时间：{now}",
            f"- 关系类型：{relation_type}",
            f"- 稳定称呼：{preferred_address or '未设置'}",
        ]
        if emp.department:
            lines.append(f"- 部门：{emp.department}")
        if emp.role:
            lines.append(f"- 岗位：{emp.role}")
        if emp.personality_summary:
            lines.extend(["", "## 画像摘要", emp.personality_summary])
        if emp.communication_style:
            lines.extend(["", "## 沟通偏好", emp.communication_style])

        exportable = self.exportable_memories(memories)
        if exportable:
            lines.append("")
            lines.append("## 可归档长期记忆")
            for memory in exportable:
                content = self._strip_source_note(memory.content)
                lines.append(f"- [{memory.kind}] {content}")
        return "\n".join(lines).strip() + "\n"

    def exportable_memories(
        self,
        memories: list[EmployeeMemory],
    ) -> list[EmployeeMemory]:
        return [
            memory
            for memory in memories
            if memory.kind in self.exportable_kinds
            and "explicit_correction" not in memory.content
        ]

    async def sync_to_astrbot_kb(
        self,
        kb_manager: Any,
        document: str,
        *,
        file_name: str,
    ) -> EmployeeMemoryArchiveResult:
        if kb_manager is None:
            return EmployeeMemoryArchiveResult(
                "skipped", message="kb_manager not ready"
            )
        helper = None
        kb_name = ""
        for name in self.kb_names:
            try:
                helper = await kb_manager.get_kb_by_name(name)
            except Exception:
                helper = None
            if helper is not None:
                kb_name = name
                break
        if helper is None:
            return EmployeeMemoryArchiveResult("skipped", message="no target kb found")
        await helper.upload_document(
            file_name=file_name,
            file_content=None,
            file_type="md",
            pre_chunked_text=[document],
        )
        return EmployeeMemoryArchiveResult("synced", target=kb_name)

    def export_to_nas(
        self,
        document: str,
        *,
        file_name: str,
        inbox_dir: Path | None = None,
    ) -> EmployeeMemoryArchiveResult:
        target_dir = inbox_dir or self.nas_inbox_dir
        if target_dir is None:
            return EmployeeMemoryArchiveResult("skipped", message="nas inbox not set")
        out_dir = target_dir / "employee_memory_identity"
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / file_name
        target.write_text(document, encoding="utf-8")
        return EmployeeMemoryArchiveResult("synced", target=str(target))

    def archive_file_name(self, emp: Employee, preferred_address: str) -> str:
        label = emp.display_name or preferred_address or emp.open_id[:8] or "employee"
        safe = re.sub(r"[^0-9A-Za-z一-龥._-]+", "-", label).strip("-")
        return f"employee-memory-{safe or 'employee'}.md"

    def _strip_source_note(self, content: str) -> str:
        return re.sub(r"（出自：.*?）", "", content).strip()
