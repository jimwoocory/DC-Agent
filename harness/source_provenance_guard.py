from __future__ import annotations

import math
from typing import Any

_STRICT_WORKFLOW_KINDS = {"department_workflow", "content_sop_workflow"}
_SOURCE_CITATION_FIELDS = {"source_path", "url", "source_id", "id"}
_FEISHU_HIT_FIELDS = {"id", "url", "source_id"}
_GENERIC_SOURCE_FIELDS = _SOURCE_CITATION_FIELDS | _FEISHU_HIT_FIELDS
_GENERIC_PROVENANCE_KEYS = {"knowledge_sources", "sources", "provenance"}


def completion_requires_source_provenance(
    task: Any,
    result: dict[str, Any] | None,
) -> bool:
    return payload_requires_source_provenance(
        getattr(task, "payload", {}) or {}, result
    )


def payload_requires_source_provenance(
    payload: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> bool:
    payload = payload or {}
    result = result or {}

    return (
        payload.get("strict_source_provenance_required") is True
        or payload.get("employee_facing") is True
        or result.get("strict_source_provenance_required") is True
        or result.get("employee_facing") is True
        or _truth_ready_with_requirements(payload)
        or _strict_fact_workflow(payload)
    )


def completion_has_source_provenance(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return False

    if _has_concrete_entries(result.get("source_citations"), _SOURCE_CITATION_FIELDS):
        return True
    if _has_concrete_entries(result.get("hits"), _FEISHU_HIT_FIELDS):
        return True
    return any(
        _has_concrete_entries(result.get(key), _GENERIC_SOURCE_FIELDS)
        for key in _GENERIC_PROVENANCE_KEYS
    )


def assert_completion_source_provenance(
    task: Any,
    result: dict[str, Any] | None,
) -> None:
    if not completion_requires_source_provenance(task, result):
        return
    if completion_has_source_provenance(result):
        return
    raise RuntimeError(
        "cannot complete employee-facing strict-source task without concrete source "
        f"provenance: task_id={getattr(task, 'task_id', '<unknown>')!r}"
    )


def assert_payload_completion_source_provenance(
    task_id: str,
    payload: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> None:
    if not payload_requires_source_provenance(payload, result):
        return
    if completion_has_source_provenance(result):
        return
    raise RuntimeError(
        "cannot complete employee-facing strict-source task without concrete source "
        f"provenance: task_id={task_id!r}"
    )


def _truth_ready_with_requirements(payload: dict[str, Any]) -> bool:
    return payload.get("truth_status") == "ready_for_execution" and bool(
        payload.get("truth_requirements")
    )


def _strict_fact_workflow(payload: dict[str, Any]) -> bool:
    return (
        payload.get("workflow_kind") in _STRICT_WORKFLOW_KINDS
        and payload.get("generation_allowed") is True
    )


def _has_concrete_entries(value: Any, source_fields: set[str]) -> bool:
    if isinstance(value, list):
        return any(_entry_has_source_field(entry, source_fields) for entry in value)
    if isinstance(value, dict):
        return _entry_has_source_field(value, source_fields) or any(
            _entry_has_source_field(entry, source_fields) for entry in value.values()
        )
    return False


def _entry_has_source_field(entry: Any, source_fields: set[str]) -> bool:
    if not isinstance(entry, dict):
        return False
    return any(_has_concrete_value(entry.get(field)) for field in source_fields)


def _has_concrete_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, float):
        return math.isfinite(value) and value > 0
    return False
