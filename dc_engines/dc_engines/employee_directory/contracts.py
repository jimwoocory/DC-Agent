"""员工目录数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MemoryKind = Literal[
    "preference",
    "fact",
    "context",
    "skill",
    "role",
    "persona_evidence",
    "persona",
]
RelationType = Literal["boss", "manager", "employee", "unknown"]


@dataclass(slots=True)
class Employee:
    """单条员工档案。``open_id`` 是飞书 app-scoped 用户 ID，跨会话稳定。"""

    open_id: str
    platform_id: str = ""  # 哪个 AstrBot lark adapter 见过此人
    display_name: str = ""  # 显示名（首次接触时为空，等自我介绍）
    department: str = ""
    role: str = ""  # 业务/执行/设计/文案/负责人/财务/...
    relation_type: RelationType = "employee"
    preferred_address: str = ""
    honorific_policy: str = "formal"
    personality_summary: str = ""
    communication_style: str = ""
    persona_evidence_count: int = 0
    persona_updated_at: str = ""
    skill_tags: list[str] = field(default_factory=list)
    preferences: dict = field(default_factory=dict)
    first_seen_at: str = ""
    last_seen_at: str = ""
    interaction_count: int = 0

    @property
    def is_anonymous(self) -> bool:
        """是否还没自我介绍。"""
        return not self.display_name


@dataclass(slots=True)
class EmployeeMemory:
    """一条员工长期记忆。"""

    memory_id: str
    open_id: str
    kind: MemoryKind
    content: str
    relevance: float = 0.5  # 0-1，注入 LLM 时按此排序
    created_at: str = ""
