"""W2 / 2A-2 任务提取引擎。

从聊天消息中抽出结构化任务（事项 + 负责人 + 截止时间 + 优先级）。

子模块：
- ``contracts`` — ExtractedTask 数据模型
- ``deadline_parser`` — 中英文相对时间解析（"周三前" / "明天 9:00" / "5月20日"）
- ``extractor`` — LLM-backed JSON 抽取（system prompt + 容错解析）
"""

from .contracts import ExtractedTask
from .deadline_parser import parse_deadline
from .extractor import TASK_EXTRACTOR_SYSTEM_PROMPT, extract_tasks_from_messages

__all__ = [
    "TASK_EXTRACTOR_SYSTEM_PROMPT",
    "ExtractedTask",
    "extract_tasks_from_messages",
    "parse_deadline",
]
