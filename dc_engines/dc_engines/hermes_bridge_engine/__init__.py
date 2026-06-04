"""Hermes Bridge supporting engines (W0 G2 / Phase 0.3).

- ``callback_dispatcher`` — retry-with-backoff dispatcher for Hermes → AstrBot
  result callbacks; classifies HTTP errors into retriable / permanent and
  hands permanent failures to the DLQ.
- ``dlq_logger`` — append-only JSONL dead-letter queue with single-backup
  rotation (10MB default).
"""

from .callback_dispatcher import (
    HermesCallbackDispatcher,
    PermanentSendError,
    RetriableSendError,
    classify_http_status,
    verify_hmac_signature,
)
from .dlq_logger import HermesDLQLogger, build_dlq_record

__all__ = [
    "HermesCallbackDispatcher",
    "HermesDLQLogger",
    "PermanentSendError",
    "RetriableSendError",
    "build_dlq_record",
    "classify_http_status",
    "verify_hmac_signature",
]
