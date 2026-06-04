"""W2 / 2A-2 任务抽取数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

TaskPriority = Literal["high", "normal", "low"]


@dataclass(slots=True)
class ExtractedTask:
    """从聊天里抽出的一条任务。

    description / assignee_hint / deadline_raw 是 LLM 输出的原文本；
    assignee_user_id / deadline 是后处理解析结果（解析失败为 None）。
    """

    description: str
    """事项描述（一句话）。"""

    priority: TaskPriority = "normal"
    """优先级。"""

    confidence: float = 0.0
    """LLM 自评置信度 0.0-1.0。"""

    assignee_hint: str | None = None
    """原文中的负责人提示（"@张三" / "我" / "小李" / None）。"""

    assignee_user_id: str | None = None
    """匹配后的内部 user_id（可选，需要 user_directory 才能解析）。"""

    deadline_raw: str | None = None
    """原文中的时间表达（"周三前" / "明天" / "5月20日" / None）。"""

    deadline: datetime | None = None
    """解析后的截止时间（解析失败为 None）。"""

    source_message_ts: str | None = None
    """来源消息时间戳（debug 用）。"""

    extras: dict = field(default_factory=dict)
    """LLM 多带的额外字段，原样保存。"""

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "priority": self.priority,
            "confidence": self.confidence,
            "assignee_hint": self.assignee_hint,
            "assignee_user_id": self.assignee_user_id,
            "deadline_raw": self.deadline_raw,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "source_message_ts": self.source_message_ts,
            "extras": dict(self.extras),
        }
