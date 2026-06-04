"""飞书写操作数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ChatCreationRequest:
    name: str
    owner_open_id: str
    member_open_ids: list[str] = field(default_factory=list)
    description: str = ""
    chat_mode: str = "group"  # "group" / "topic"
    chat_type: str = "private"  # "private"（仅成员可见）/ "public"
    external: bool = False  # 是否允许外部成员
    set_bot_manager: bool = True  # bot 是否当管理员（方便后续推送）


@dataclass(slots=True)
class ChatCreationResult:
    success: bool
    chat_id: str | None = None
    chat_url: str | None = None
    invited_count: int = 0
    invalid_member_ids: list[str] = field(default_factory=list)
    error: str | None = None
