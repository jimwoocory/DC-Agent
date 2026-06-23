from __future__ import annotations

import re
from typing import Any

HERMES_FINAL_FAILURE_RE = re.compile(
    r"API failed after \d+ retries|Final error|Request timed out\.",
    re.IGNORECASE,
)
HERMES_FAILURE_STATUSES = {
    "failed",
    "failure",
    "error",
    "errored",
    "cancelled",
    "canceled",
}
HERMES_PROVENANCE_FIELDS = (
    "source_citations",
    "hits",
    "sources",
    "knowledge_sources",
    "provenance",
)


def callback_text_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "response", "message", "result", "content", "error"):
            text = callback_text_value(value.get(key))
            if text:
                return text
    if isinstance(value, list):
        parts = [callback_text_value(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    return ""


def callback_response_text(data: dict[str, Any]) -> str:
    for key in ("response", "message", "result"):
        text = callback_text_value(data.get(key))
        if text:
            return text
    return ""


def callback_provenance_fields(data: dict[str, Any]) -> dict[str, Any]:
    result = data.get("result")
    containers = [data]
    if isinstance(result, dict):
        containers.append(result)

    provenance: dict[str, Any] = {}
    for key in HERMES_PROVENANCE_FIELDS:
        for container in containers:
            value = container.get(key)
            if value:
                provenance[key] = value
                break
    return provenance


def callback_failure_reason(
    data: dict[str, Any],
    response_text: str,
) -> str | None:
    for key in ("status", "state"):
        value = data.get(key)
        if isinstance(value, str) and value.strip().lower() in HERMES_FAILURE_STATUSES:
            return (
                callback_text_value(data.get("error"))
                or response_text
                or f"hermes callback {key}={value.strip()}"
            )

    error_text = callback_text_value(data.get("error"))
    if error_text:
        return error_text
    if response_text and HERMES_FINAL_FAILURE_RE.search(response_text):
        return response_text
    return None


def build_harness_result(
    response_text: str,
    provenance_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "summary": response_text[:200],
        "response_preview": response_text[:500],
        "source": "hermes",
    }
    if provenance_fields:
        result.update(provenance_fields)
    return result
