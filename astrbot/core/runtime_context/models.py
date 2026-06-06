from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RuntimeContextPriority(StrEnum):
    CURRENT = "current"
    RECENT_HISTORY = "recent_history"
    REFERENCE = "reference"
    BACKGROUND = "background"


class RuntimeContextSource(StrEnum):
    CURRENT_USER = "current_user"
    CONVERSATION_HISTORY = "conversation_history"
    LONG_TERM_MEMORY = "long_term_memory"
    KNOWLEDGE_BASE = "knowledge_base"
    ATTACHMENT = "attachment"
    PLATFORM_HISTORY = "platform_history"
    SYSTEM = "system"


@dataclass(slots=True)
class RuntimeContextSection:
    text: str
    source: RuntimeContextSource
    priority: RuntimeContextPriority
    no_save: bool = False
    source_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def memory_reference(
        cls,
        text: str,
        source_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> RuntimeContextSection:
        return cls(
            text=text,
            source=RuntimeContextSource.LONG_TERM_MEMORY,
            priority=RuntimeContextPriority.REFERENCE,
            no_save=True,
            source_id=source_id,
            metadata=metadata or {},
        )
