"""Hermes Bridge supporting engines (W0 G2 / Phase 0.3).

- ``callback_dispatcher`` — retry-with-backoff dispatcher for Hermes → AstrBot
  result callbacks; classifies HTTP errors into retriable / permanent and
  hands permanent failures to the DLQ.
- ``dlq_logger`` — append-only JSONL dead-letter queue with single-backup
  rotation (10MB default).
- ``task_dispatcher`` — outbound Hermes task webhook adapter.
"""

from .callback_dispatcher import (
    HermesCallbackDispatcher,
    PermanentSendError,
    RetriableSendError,
    classify_http_status,
    verify_hmac_signature,
)
from .callback_payload import (
    HERMES_FINAL_FAILURE_RE,
    build_harness_result,
    callback_failure_reason,
    callback_provenance_fields,
    callback_response_text,
    callback_text_value,
)
from .dlq_logger import HermesDLQLogger, build_dlq_record
from .task_dispatcher import HermesTaskDispatcher

__all__ = [
    "HERMES_FINAL_FAILURE_RE",
    "HermesCallbackDispatcher",
    "HermesDLQLogger",
    "HermesTaskDispatcher",
    "PermanentSendError",
    "RetriableSendError",
    "build_harness_result",
    "build_dlq_record",
    "callback_failure_reason",
    "classify_http_status",
    "callback_provenance_fields",
    "callback_response_text",
    "callback_text_value",
    "verify_hmac_signature",
]
