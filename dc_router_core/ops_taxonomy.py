"""Ops router taxonomy（巅池-技术 DevOps 机器人专用）。

跟 business router 完全独立：
- OpsIntent 跟 business 的 RouterIntent 是不同的 enum
- 共享 RouterDecision 契约（输出同一个数据结构）
- entrypoint 按 envelope.metadata['platform_id'] 切到这边

参考 memory: project-dual-bot-router-architecture
"""

from __future__ import annotations

from enum import Enum


class OpsIntent(str, Enum):
    """运维 / 技术 / 后台 机器人的意图分类。

    明确不做的事:
    - ❌ 营销 creative / 品牌 insight (归 business router)
    - ❌ 业务 Hermes 深度分析
    - ❌ Gemini Pro 创意 / Claude OAuth 深度
    """

    SYSTEM_STATUS = "system_status"
    """后台运行数据 / Hermes / AstrBot / 飞书链路状态查询"""

    QUEUE_STATUS = "queue_status"
    """深度任务队列 / 冷却 / 429 / 失败任务告警查询"""

    ERROR_DEBUG = "error_debug"
    """报错解释、链路排障、日志解读"""

    CODE_OPS = "code_ops"
    """代码问题、修复建议、小脚本（Codex CLI gpt-5.4）"""

    DEPLOYMENT_OPS = "deployment_ops"
    """运维命令、诊断、部署、git 操作（Codex CLI gpt-5.4）"""

    QUOTA_GATE_VIEW = "quota_gate_view"
    """凭证池 / OAuth 限速 / aihubmix 用量查看"""

    OPS_FALLBACK = "ops_fallback"
    """运维分类不明 - 走 Codex CLI gpt-5.4 兜底"""
