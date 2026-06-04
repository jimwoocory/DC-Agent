"""Hermes 回群死信队列（DLQ）落盘记录器（Phase 0.3 / G2）。

设计目标：
- Hermes 异步回群链路在 N 次重试后仍失败的消息，落一行 JSON 到 DLQ。
- 单文件 + 单备份的环形轮转（默认 10MB），避免无限增长，磁盘可控。
- async 安全：内部 asyncio.Lock 串行化写入。
- 失败容错：任何写入异常不得阻断 webhook 主流程或重试链。

下游消费者：
- 运维人工或定时任务从 DLQ 重放、归档、告警。
- 后续 Dashboard 渲染失败率 / 失败原因聚类。

实现刻意与 RouterDecisionLogger 同构（轮转策略、async lock、字段顺序），
方便维护者复用心智模型。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10MB


class HermesDLQLogger:
    """Append-only JSONL DLQ logger with single-backup rotation."""

    def __init__(
        self,
        path: Path | str,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = asyncio.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(self.path.name + ".1")

    async def log(self, record: dict[str, Any]) -> None:
        """写入一条 DLQ 记录。任何 I/O 异常被吞掉以保护主流程。"""
        try:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        except (TypeError, ValueError):
            return
        encoded = line.encode("utf-8")

        async with self._lock:
            try:
                await asyncio.to_thread(self._write_with_rotation, encoded)
            except OSError:
                return

    def _write_with_rotation(self, encoded: bytes) -> None:
        try:
            current_size = self.path.stat().st_size
        except FileNotFoundError:
            current_size = 0

        if current_size + len(encoded) > self.max_bytes and current_size > 0:
            backup = self.backup_path
            try:
                if backup.exists():
                    backup.unlink()
                self.path.rename(backup)
            except OSError:
                pass

        with open(self.path, "ab") as f:
            f.write(encoded)


def build_dlq_record(
    *,
    task_id: str | None,
    target_umo: str,
    payload: dict[str, Any],
    last_error: str,
    attempt_count: int,
) -> dict[str, Any]:
    """统一构造 DLQ record dict。

    字段约定（与下游运维消费者契约）：
      ts            : float, time.time()
      task_id       : str | None, Harness 任务 ID（如果回调携带）
      target_umo    : str, 目标平台 unified_msg_origin
      payload       : dict, 原始回调负载（含 message、session_key 等）
      last_error    : str, 最后一次失败的错误描述
      attempt_count : int, 实际尝试次数（1 表示首次失败即写 DLQ）
    """
    return {
        "ts": time.time(),
        "task_id": task_id,
        "target_umo": target_umo,
        "payload": payload,
        "last_error": last_error,
        "attempt_count": int(attempt_count),
    }


__all__ = [
    "DEFAULT_MAX_BYTES",
    "HermesDLQLogger",
    "build_dlq_record",
]
