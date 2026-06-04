"""Hermes → AstrBot 回群链路的重试调度器（Phase 0.3 / G2）。

设计目标
========
- 当 Hermes 通过 webhook 回推处理结果时，bridge 通过 platform adapter
  把结果异步发回原 unified_msg_origin（umo）。该过程可能因网络抖动 /
  平台限流 / 服务端 5xx 而失败，需要带指数退避的重试与最终 DLQ 兜底。

- 调度器**与具体发送方式解耦**：通过注入 `sender` 回调（async callable
  接受 `umo, message`），既能在生产中绑定到 `Context.send_message`，
  也能在测试里替换成 mock。便于覆盖 4xx / 5xx / 超时 / 返回 False 等
  各种分支，而无需起真实的 platform adapter。

错误分类
========
sender 可以通过抛出不同异常告知 dispatcher 错误类型：

- `RetriableSendError`  : 可重试（网络异常、5xx、超时）
- `PermanentSendError`  : 不可重试（4xx、umo 找不到、参数错）

其他未识别异常按 *permanent* 处理 —— 安全失败优于无尽重试。

重试策略
========
- 最多 `max_attempts`（默认 3）次尝试，含首次。
- 每次失败后按 `retry_delays`（默认 1s / 2s / 4s）退避，最后一次不再 sleep。
- 4xx / permanent 错误：立即停止重试，直接落 DLQ。

HMAC
====
`verify_hmac_signature` 提供对入站 webhook 的 X-Hub-Signature-256 校验，
与发送方向的签名算法对称（hmac-sha256 over raw body）。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .dlq_logger import HermesDLQLogger, build_dlq_record

logger = logging.getLogger(__name__)


# ── 错误分类 ──────────────────────────────────────────────────────────────────


class RetriableSendError(Exception):
    """发送失败但可重试（网络、5xx、超时）。"""


class PermanentSendError(Exception):
    """发送失败且不应重试（4xx、参数错、umo 不存在）。"""


def classify_http_status(status: int) -> type[Exception] | None:
    """根据 HTTP 状态码返回对应的错误类型（5xx → 可重试，4xx → 永久失败）。

    如果传入的状态码不属于 4xx/5xx，返回 None（让调用方自行决定）。
    """
    if 500 <= status < 600:
        return RetriableSendError
    if 400 <= status < 500:
        return PermanentSendError
    return None


# ── 结果对象 ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SendOutcome:
    success: bool
    attempts: int
    last_error: str | None
    dlq_written: bool


# ── HMAC ──────────────────────────────────────────────────────────────────────


def verify_hmac_signature(
    secret: str,
    body: bytes,
    signature_header: str,
) -> bool:
    """校验 X-Hub-Signature-256 头部。

    期望 header 形如 `sha256=<hex>`。空 secret 或空 header 视为校验失败，
    由调用方决定是否允许"无签名"穿透（grace 模式）。
    """
    if not secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected_hex = signature_header[len("sha256=") :].strip()
    if not expected_hex:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected_hex)


# ── 主调度器 ──────────────────────────────────────────────────────────────────


SenderCallable = Callable[[str, str], Awaitable[None]]


class HermesCallbackDispatcher:
    """Retry + DLQ wrapper for Hermes → 平台 的回群发送。"""

    DEFAULT_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)

    def __init__(
        self,
        *,
        sender: SenderCallable,
        dlq_logger: HermesDLQLogger,
        max_attempts: int = 3,
        retry_delays: tuple[float, ...] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._sender = sender
        self._dlq_logger = dlq_logger
        self._max_attempts = max_attempts
        self._retry_delays = retry_delays or self.DEFAULT_RETRY_DELAYS
        self._sleep = sleep or asyncio.sleep

    async def send_with_retry(
        self,
        *,
        target_umo: str,
        message: str,
        task_id: str | None = None,
        extra_payload: dict | None = None,
    ) -> SendOutcome:
        """尝试发送 message 到 target_umo，失败按策略重试，最终落 DLQ。

        返回 SendOutcome 描述结果；任何分支都不抛出（除非 DLQ logger 自身
        异常，但 HermesDLQLogger 内部已经吞掉 I/O 错误）。
        """
        last_error: str | None = None
        attempt = 0

        for attempt in range(1, self._max_attempts + 1):
            try:
                await self._sender(target_umo, message)
                return SendOutcome(
                    success=True,
                    attempts=attempt,
                    last_error=None,
                    dlq_written=False,
                )
            except PermanentSendError as exc:
                last_error = f"permanent: {exc}"
                logger.warning(
                    "[HermesCallback] permanent error for umo=%s on attempt %s: %s",
                    target_umo,
                    attempt,
                    exc,
                )
                break
            except RetriableSendError as exc:
                last_error = f"retriable: {exc}"
                logger.warning(
                    "[HermesCallback] retriable error for umo=%s on attempt %s/%s: %s",
                    target_umo,
                    attempt,
                    self._max_attempts,
                    exc,
                )
                if attempt < self._max_attempts:
                    await self._sleep(self._pick_delay(attempt))
                    continue
                break
            except Exception as exc:
                last_error = f"unknown: {type(exc).__name__}: {exc}"
                logger.error(
                    "[HermesCallback] unexpected error for umo=%s on attempt %s: %s",
                    target_umo,
                    attempt,
                    exc,
                )
                break

        dlq_written = await self._write_dlq(
            task_id=task_id,
            target_umo=target_umo,
            message=message,
            last_error=last_error or "unknown",
            attempt_count=attempt,
            extra_payload=extra_payload,
        )
        return SendOutcome(
            success=False,
            attempts=attempt,
            last_error=last_error,
            dlq_written=dlq_written,
        )

    def _pick_delay(self, attempt: int) -> float:
        """从重试延迟序列中按 attempt（1-based）取值，超界返回最后一项。"""
        idx = min(attempt - 1, len(self._retry_delays) - 1)
        return self._retry_delays[idx]

    async def _write_dlq(
        self,
        *,
        task_id: str | None,
        target_umo: str,
        message: str,
        last_error: str,
        attempt_count: int,
        extra_payload: dict | None,
    ) -> bool:
        payload: dict = {"message": message}
        if extra_payload:
            payload.update(extra_payload)
        record = build_dlq_record(
            task_id=task_id,
            target_umo=target_umo,
            payload=payload,
            last_error=last_error,
            attempt_count=attempt_count,
        )
        try:
            await self._dlq_logger.log(record)
            return True
        except Exception as exc:
            logger.error("[HermesCallback] DLQ write failed: %s", exc)
            return False


__all__ = [
    "HermesCallbackDispatcher",
    "PermanentSendError",
    "RetriableSendError",
    "SendOutcome",
    "classify_http_status",
    "verify_hmac_signature",
]
