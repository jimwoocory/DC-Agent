"""Preprocessing subsystem — pure platform-level guards.

每个模块只暴露 ``async def try_handle(...)`` 接口；
返回 True 时表示已接管消息（dispatch 不再继续），否则返回 False。

模块列表:
- ``chitchat``: 短确认 / 寒暄 / 自我介绍 — 命中后直接 reply
- ``card_action``: legacy CLI 排队卡 / department_memory 卡片回调
- ``context_alignment``: 回复失败/内部上下文但当前像新任务时先提示对齐
- ``department_memory``: 部门记忆激活提示 (suggest / confirm / dismiss)
- ``sop_signal``: 员工处理习惯低打扰确认并进入记忆治理
- ``assistant_tone``: 业务请求注入 tone template (set_extra only)
- ``media_route``: 图片/视频生成走 media SOP
- ``feishu_channel``: 飞书 channel agent 固定 provider 路由
- ``truth_intake``: 真实性资料校验 (引用 truth_intake.py 兼容旧路径)
"""

from .assistant_tone import try_inject_assistant_tone
from .assistant_workbench import try_handle_assistant_workbench
from .card_action import try_handle_card_action
from .chitchat import try_handle_chitchat
from .context_alignment import try_handle_context_alignment
from .department_memory import (
    DepartmentMemoryDecision,
    try_handle_department_memory,
)
from .feishu_channel import try_apply_feishu_channel_route
from .media_route import (
    is_source_image_edit_request,
    try_handle_media_route,
    try_handle_source_image_edit,
)
from .sop_signal import try_capture_sop_signal

__all__ = [
    "DepartmentMemoryDecision",
    "is_source_image_edit_request",
    "try_apply_feishu_channel_route",
    "try_capture_sop_signal",
    "try_handle_assistant_workbench",
    "try_handle_card_action",
    "try_handle_chitchat",
    "try_handle_context_alignment",
    "try_handle_department_memory",
    "try_handle_media_route",
    "try_handle_source_image_edit",
    "try_inject_assistant_tone",
]
