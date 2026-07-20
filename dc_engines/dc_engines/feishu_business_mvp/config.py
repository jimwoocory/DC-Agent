"""Configuration loader for Feishu business MVP workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import BitableLocation, BusinessMvpConfig

FORBIDDEN_SECRET_KEYS = {
    "app_secret",
    "appsecret",
    "encrypt_key",
    "encryption_key",
    "refresh_token",
    "tenant_access_token",
    "user_access_token",
    "verification_token",
}


def load_business_mvp_config(path: str | Path | None = None) -> BusinessMvpConfig:
    """Load redacted business workflow configuration.

    Args:
        path: Optional JSON config path. Missing files return default config.

    Returns:
        BusinessMvpConfig parsed from JSON.
    """

    if path is None:
        path = Path("/Users/dianchi/DC-Agent/data/config/feishu_business_mvp.json")
    config_path = Path(path)
    if not config_path.exists():
        return BusinessMvpConfig()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return BusinessMvpConfig()
    if not isinstance(raw, dict):
        return BusinessMvpConfig()
    return business_mvp_config_from_dict(raw)


def load_business_mvp_config_payload(path: str | Path | None = None) -> dict[str, Any]:
    """Load raw business config payload for preflight validation.

    Args:
        path: Optional JSON config path.

    Returns:
        Raw config dict, or an empty dict when missing/invalid.
    """

    if path is None:
        path = Path("/Users/dianchi/DC-Agent/data/config/feishu_business_mvp.json")
    config_path = Path(path)
    if not config_path.exists():
        return {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def business_mvp_config_from_dict(raw: dict[str, Any]) -> BusinessMvpConfig:
    """Build BusinessMvpConfig from a dict.

    Args:
        raw: Raw config dict.

    Returns:
        Parsed BusinessMvpConfig.
    """

    return BusinessMvpConfig(
        enabled=bool(raw.get("enabled", True)),
        db_path=str(raw.get("db_path") or "data/feishu_business_mvp.db"),
        asset_table=_location(raw.get("asset_table")),
        finance_table=_location(raw.get("finance_table")),
        onboarding_table=_location(raw.get("onboarding_table")),
        approval_codes=_string_dict(raw.get("approval_codes")),
        notification_targets=_string_dict(raw.get("notification_targets")),
        required_finance_attachments=_string_list_dict(
            raw.get("required_finance_attachments")
        ),
        default_onboarding_tasks=_string_list(
            raw.get("default_onboarding_tasks"),
            ["账号开通", "办公用品准备", "工位确认", "入职资料收集"],
        ),
        sync_window_hours=max(1, int(raw.get("sync_window_hours") or 168)),
    )


def validate_business_mvp_config(
    config: BusinessMvpConfig,
    raw_payload: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Validate business config for Feishu MVP preflight.

    Args:
        config: Parsed business config.
        raw_payload: Optional raw payload used to detect forbidden secret keys.

    Returns:
        List of issue dicts with severity, code, path, and message.
    """

    issues: list[dict[str, str]] = []
    for path in find_forbidden_secret_keys(raw_payload or {}):
        issues.append(
            {
                "severity": "error",
                "code": "forbidden_secret_key",
                "path": path,
                "message": "业务配置不得保存 app_secret、access_token、verification_token 等敏感凭证。",
            }
        )
    if not config.asset_table.configured:
        issues.append(
            {
                "severity": "warning",
                "code": "asset_table_missing",
                "path": "asset_table",
                "message": "未配置办公用品多维表格 app_token/table_id，资产同步会跳过。",
            }
        )
    if not config.onboarding_table.configured:
        issues.append(
            {
                "severity": "warning",
                "code": "onboarding_table_missing",
                "path": "onboarding_table",
                "message": "未配置入职任务多维表格 app_token/table_id，入职同步会跳过。",
            }
        )
    if not config.approval_codes:
        issues.append(
            {
                "severity": "warning",
                "code": "approval_codes_missing",
                "path": "approval_codes",
                "message": "未配置 approval_code，财务审批同步会跳过。",
            }
        )
    for key in ("admin_chat_id", "finance_chat_id", "management_chat_id"):
        if not config.notification_targets.get(key):
            issues.append(
                {
                    "severity": "warning",
                    "code": f"{key}_missing",
                    "path": f"notification_targets.{key}",
                    "message": f"未配置 {key}，对应提醒或周报会记录为 skipped。",
                }
            )
    return issues


def find_forbidden_secret_keys(
    raw: Any,
    *,
    prefix: str = "",
) -> list[str]:
    """Find forbidden secret-like keys while allowing business app_token fields.

    Args:
        raw: Raw JSON-like value.
        prefix: Current key path.

    Returns:
        Paths containing forbidden secret keys.
    """

    found: list[str] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            key_text = str(key)
            key_norm = key_text.replace("-", "_").lower()
            path = f"{prefix}.{key_text}" if prefix else key_text
            if key_norm in FORBIDDEN_SECRET_KEYS or key_norm.endswith("_secret"):
                found.append(path)
            found.extend(find_forbidden_secret_keys(value, prefix=path))
    elif isinstance(raw, list):
        for index, item in enumerate(raw):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            found.extend(find_forbidden_secret_keys(item, prefix=path))
    return found


def _location(raw: Any) -> BitableLocation:
    """Parse one Bitable location.

    Args:
        raw: Raw location dict.

    Returns:
        BitableLocation.
    """

    data = raw if isinstance(raw, dict) else {}
    return BitableLocation(
        app_token=str(data.get("app_token") or ""),
        table_id=str(data.get("table_id") or ""),
        view_id=str(data.get("view_id") or ""),
        primary_key=str(data.get("primary_key") or ""),
    )


def _string_dict(raw: Any) -> dict[str, str]:
    """Parse a string-to-string dict.

    Args:
        raw: Raw dict.

    Returns:
        Dict with non-empty string keys and values.
    """

    if not isinstance(raw, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if str(key).strip() and str(value).strip()
    }


def _string_list_dict(raw: Any) -> dict[str, list[str]]:
    """Parse a string-to-string-list dict.

    Args:
        raw: Raw dict.

    Returns:
        Dict with string list values.
    """

    if not isinstance(raw, dict):
        return {}
    return {
        str(key): _string_list(value, [])
        for key, value in raw.items()
        if str(key).strip()
    }


def _string_list(raw: Any, default: list[str]) -> list[str]:
    """Parse a string list.

    Args:
        raw: Raw list value.
        default: Returned when raw is not a list or becomes empty.

    Returns:
        Clean string list.
    """

    if not isinstance(raw, list):
        return list(default)
    values = [str(item).strip() for item in raw if str(item).strip()]
    return values or list(default)
