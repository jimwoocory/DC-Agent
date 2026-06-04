"""飞书 SDK 客户端单例 + 调用统计 + 重试。

为什么要单例：
- ``lark.Client`` 内部有 tenant_access_token 缓存，不同实例之间不复用
  → 多处独立 build client 会导致 token 频繁刷新 + N 倍调用配额
- 凭证只该加载一次（避免重复读 yaml）

为什么要调用统计：
- 跟 dc-watchdog 配合：调用量 / 失败率作为业务级 SLI 喂回 harness memory
- 飞书有限流，统计帮我们看"今天烧了多少配额"
- 出 bug 时 codex 诊断能拿到"最近调了啥 + 哪个失败了"

调用方式：

>>> from dc_engines.feishu_hub import get_client
>>> client = get_client()
>>> if client is None:
...     return  # 凭证缺失，业务降级
>>> resp = await client.docx.v1.document.aget(req)
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import lark_oapi as lark

from .credentials import FeishuCredentials, load_credentials

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HubStats:
    """运行时调用统计（轻量内存计数，进程重启清零）。"""

    started_at: float = field(default_factory=time.time)
    total_calls: int = 0
    total_errors: int = 0
    calls_by_method: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    errors_by_method: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    last_error: str = ""
    last_error_at: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        """返一个 plain dict 用于喂 harness memory / dashboard 显示。"""
        return {
            "started_at": self.started_at,
            "uptime_seconds": int(time.time() - self.started_at),
            "total_calls": self.total_calls,
            "total_errors": self.total_errors,
            "error_rate": (
                round(self.total_errors / self.total_calls, 4)
                if self.total_calls
                else 0.0
            ),
            "calls_by_method": dict(self.calls_by_method),
            "errors_by_method": dict(self.errors_by_method),
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
        }


class FeishuHub:
    """全 process 单例。第一次拿凭证 + build client，后续复用。"""

    _instance: FeishuHub | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        # 单例约束在 _get_instance 强制；这里只做字段初始化，幂等
        self._credentials: FeishuCredentials | None = None
        self._client: lark.Client | None = None
        self._stats = HubStats()
        self._init_lock = threading.Lock()
        self._initialized = False

    @classmethod
    def _get_instance(cls) -> FeishuHub:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_for_test(cls) -> None:
        """测试用——清空单例。生产代码不该调。"""
        with cls._lock:
            cls._instance = None

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self._credentials = load_credentials()
            if self._credentials and self._credentials.enable:
                self._client = (
                    lark.Client.builder()
                    .app_id(self._credentials.app_id)
                    .app_secret(self._credentials.app_secret)
                    .log_level(lark.LogLevel.WARNING)
                    .build()
                )
                logger.info(
                    "[feishu_hub] 初始化 OK，凭证来自 %s",
                    self._credentials.source,
                )
            else:
                logger.info(
                    "[feishu_hub] 凭证缺失 / 已禁用 → client=None，上层走 disabled"
                )
            self._initialized = True

    @property
    def credentials(self) -> FeishuCredentials | None:
        self._ensure_initialized()
        return self._credentials

    @property
    def client(self) -> lark.Client | None:
        self._ensure_initialized()
        return self._client

    @property
    def enabled(self) -> bool:
        self._ensure_initialized()
        return self._client is not None

    @property
    def stats(self) -> HubStats:
        return self._stats

    def record_call(self, method: str, error: Exception | None = None) -> None:
        """业务层调用飞书 API 后记一笔（成功 / 失败都记）。

        简单做法：上层在 try/except 里手动调；后续可以做装饰器自动调。
        """
        self._stats.total_calls += 1
        self._stats.calls_by_method[method] += 1
        if error is not None:
            self._stats.total_errors += 1
            self._stats.errors_by_method[method] += 1
            self._stats.last_error = f"{type(error).__name__}: {error}"
            self._stats.last_error_at = time.time()


# ───────────────────────── 公共入口 ─────────────────────────


def get_hub() -> FeishuHub:
    """返单例 FeishuHub。"""
    return FeishuHub._get_instance()


def get_client() -> lark.Client | None:
    """便捷入口：返 lark.Client 或 None（凭证缺失）。"""
    return get_hub().client


def get_credentials() -> FeishuCredentials | None:
    """便捷入口：返凭证对象或 None。"""
    return get_hub().credentials


def is_enabled() -> bool:
    """便捷入口：飞书是否可用（凭证齐 + 已 build client）。"""
    return get_hub().enabled


async def call(method: str, coro):
    """便捷封装：自动 record_call。

    >>> resp = await call("docx.document.get", client.docx.v1.document.aget(req))

    成功时返 resp，失败时 re-raise 并已记录到 stats。
    """
    hub = get_hub()
    try:
        result = await coro
        hub.record_call(method, error=None)
        return result
    except Exception as exc:
        hub.record_call(method, error=exc)
        raise


# 让 sync 代码也能用 record_call 装饰风格（feishu_sync.py 在 watcher 里跑可能是 sync）
def call_sync(method: str, fn, *args, **kwargs):
    hub = get_hub()
    try:
        result = fn(*args, **kwargs)
        hub.record_call(method, error=None)
        return result
    except Exception as exc:
        hub.record_call(method, error=exc)
        raise


__all__ = [
    "FeishuHub",
    "HubStats",
    "get_hub",
    "get_client",
    "get_credentials",
    "is_enabled",
    "call",
    "call_sync",
]
