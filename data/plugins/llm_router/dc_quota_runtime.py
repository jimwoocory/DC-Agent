"""QuotaGate 运行时单例（步骤 4 / dc-harness 基础设施）。

提供 lazy 初始化的 QuotaGate 实例。SQLite 数据库放在 data/dc_harness.db。

设计原则:
- enabled=false 时永远不被调用（dc_router_adapter 自己保证）
- 第一次调 get_quota_gate() 时 init 表（WAL 模式）+ 创建单例
- 后续调用复用同一实例
- ToS 提醒: harness/resources.py 当前包含 Claude OAuth 资源，
  但只要 adapter 不真把请求路由到 CLAUDE_* 资源，就不会触发 ToS 调用。
  这一层 quota_gate 是中立的——它只管资源状态机，不决定调哪个模型。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

# 让 `from harness import ...` 从 DC-Agent 顶层 import
_DC_AGENT_ROOT = Path("/Users/dianchi/DC-Agent")
if str(_DC_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_DC_AGENT_ROOT))

from astrbot.api import logger  # noqa: E402

_QUOTA_GATE_INSTANCE: Any = None
_INIT_LOCK = asyncio.Lock()

DEFAULT_DB_PATH = "/Users/dianchi/DC-Agent/data/dc_harness.db"


async def get_quota_gate(db_path: str | None = None):
    """Lazy 初始化 QuotaGate 单例。

    第一次调用会 init SQLite 表 + 预置默认资源行。
    后续调用直接返回缓存实例。

    Returns:
        harness.QuotaGate 实例
    """
    global _QUOTA_GATE_INSTANCE
    if _QUOTA_GATE_INSTANCE is not None:
        return _QUOTA_GATE_INSTANCE

    async with _INIT_LOCK:
        # double-check 防并发重复初始化
        if _QUOTA_GATE_INSTANCE is not None:
            return _QUOTA_GATE_INSTANCE

        from harness import QuotaGate

        actual_db = db_path or DEFAULT_DB_PATH
        gate = QuotaGate(actual_db)
        await gate.store.init()
        _QUOTA_GATE_INSTANCE = gate
        logger.info("[dc-quota] QuotaGate 已初始化 · db=%s", actual_db)

    return _QUOTA_GATE_INSTANCE


def reset_quota_gate_for_test() -> None:
    """测试用：清空单例。生产代码不应该调这个。"""
    global _QUOTA_GATE_INSTANCE
    _QUOTA_GATE_INSTANCE = None
