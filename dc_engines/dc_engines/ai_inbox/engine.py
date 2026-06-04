from __future__ import annotations

import re
from dataclasses import dataclass

from .contracts import InboxItem, InboxItemCategory, InboxItemCreateRequest
from .store import InboxStore

_TASK_RE = re.compile(
    r"(待办|提醒|任务|截止|周[一二三四五六日天]|明天|后天|跟进|推进|负责人|完成|todo)",
    re.IGNORECASE,
)
_FEEDBACK_RE = re.compile(
    r"(不行|不对|不好用|没解决|太慢|卡住|失败|报错|建议|反馈|问题|缺少|不够)",
    re.IGNORECASE,
)
_MATERIAL_RE = re.compile(
    r"(资料|材料|素材|文件|附件|截图|链接|原文|补充|飞书文档|知识库)",
    re.IGNORECASE,
)
_ESCALATION_RE = re.compile(
    r"(hermes|深度|深挖|深入|重新查|查清楚|升级处理|交给)",
    re.IGNORECASE,
)
_REQUEST_RE = re.compile(
    r"(帮我|麻烦|请|需要|做|写|生成|整理|总结|分析|判断|确认|看一下|查一下)",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(r"(\?|？|怎么|如何|为什么|是不是|能不能|可以吗)")

ACTIONABLE_CATEGORIES: set[InboxItemCategory] = {
    "request",
    "task",
    "feedback",
    "material",
    "escalation",
}


@dataclass(slots=True)
class AIInboxEngine:
    store: InboxStore

    def classify(self, text: str) -> InboxItemCategory:
        normalized = text.strip()
        if not normalized:
            return "other"
        if _ESCALATION_RE.search(normalized):
            return "escalation"
        if _MATERIAL_RE.search(normalized):
            return "material"
        if _TASK_RE.search(normalized):
            return "task"
        if _FEEDBACK_RE.search(normalized):
            return "feedback"
        if _REQUEST_RE.search(normalized):
            return "request"
        if _QUESTION_RE.search(normalized):
            return "question"
        return "other"

    def is_actionable(self, category: InboxItemCategory) -> bool:
        return category in ACTIONABLE_CATEGORIES

    async def create_item(self, request: InboxItemCreateRequest) -> InboxItem:
        return await self.store.create_item(request)
