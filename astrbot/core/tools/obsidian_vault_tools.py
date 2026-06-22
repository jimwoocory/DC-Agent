"""Read-only Obsidian vault tools backed by the controlled vault wrapper."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from importlib import import_module, invalidate_caches
from pathlib import Path
from typing import Any

from astrbot.api import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .registry import builtin_tool

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DC_ENGINES_ROOT = _PROJECT_ROOT / "dc_engines"
_OBSIDIAN_VAULT_TOOL_CONFIG = {
    "provider_settings.obsidian_vault_automation.enabled": True,
}
_READ_ONLY_OPERATIONS = {"list", "read", "search", "metadata", "frontmatter"}


def _json_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _load_vault_module() -> Any:
    path = str(_DC_ENGINES_ROOT)
    if path not in sys.path:
        sys.path.insert(0, path)
    invalidate_caches()
    namespace = sys.modules.get("dc_engines")
    expected = str(_DC_ENGINES_ROOT / "dc_engines")
    if namespace is not None and getattr(namespace, "__file__", None) is None:
        locations = {str(item) for item in getattr(namespace, "__path__", ())}
        if expected not in locations:
            sys.modules.pop("dc_engines", None)
    return import_module("dc_engines.obsidian_vault_automation")


def _actor_from_context(context: ContextWrapper[AstrAgentContext]) -> str:
    event = context.context.event
    sender_id = ""
    get_sender_id = getattr(event, "get_sender_id", None)
    if callable(get_sender_id):
        sender_id = str(get_sender_id() or "").strip()
    role = str(getattr(event, "role", "") or "").strip()
    if sender_id and role:
        return f"astrbot:{role}:{sender_id}"
    if sender_id:
        return f"astrbot:{sender_id}"
    return "astrbot:unknown"


def _vault_settings(context: ContextWrapper[AstrAgentContext]) -> dict[str, Any]:
    umo = context.context.event.unified_msg_origin
    config = context.context.context.get_config(umo=umo) or {}
    provider_settings = config.get("provider_settings", {})
    settings = provider_settings.get("obsidian_vault_automation", {})
    return settings if isinstance(settings, dict) else {}


def _build_wrapper(
    context: ContextWrapper[AstrAgentContext],
) -> Any:
    settings = _vault_settings(context)
    if settings.get("enabled") is not True:
        raise PermissionError("Obsidian vault automation is disabled.")

    vault_roots = _string_list(settings.get("vault_roots"))
    if not vault_roots:
        raise PermissionError("No Obsidian vault roots are configured.")

    audit_log_path = settings.get("audit_log_path")
    if not str(audit_log_path or "").strip():
        audit_log_path = (
            Path(get_astrbot_data_path()) / "obsidian_vault_automation" / "audit.jsonl"
        )

    max_read_bytes = int(settings.get("max_read_bytes") or 256 * 1024)
    vault_module = _load_vault_module()
    return vault_module.ObsidianVaultAutomation(
        vault_module.VaultAutomationConfig(
            vault_roots=tuple(vault_roots),
            audit_log_path=audit_log_path,
            actor=_actor_from_context(context),
            max_read_bytes=max_read_bytes,
        )
    )


@builtin_tool(config=_OBSIDIAN_VAULT_TOOL_CONFIG)
@dataclass
class ObsidianVaultTool(FunctionTool[AstrAgentContext]):
    name: str = "astrbot_obsidian_vault"
    description: str = (
        "Read configured Obsidian vaults through a controlled read-only wrapper. "
        "Supports list, read, search, metadata, and frontmatter only. "
        "Never use this tool for writes, deletes, shell commands, obsidian-cli, or defuddle execution."
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["list", "read", "search", "metadata", "frontmatter"],
                    "description": "Read-only vault operation to perform.",
                },
                "path": {
                    "type": "string",
                    "description": "Vault-relative path. Absolute paths must still be inside a configured vault root.",
                    "default": "",
                },
                "query": {
                    "type": "string",
                    "description": "Search query. Required for search.",
                    "default": "",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "List nested vault files for the list operation.",
                    "default": False,
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum search results to return.",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                },
            },
            "required": ["operation"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        operation: str,
        path: str = "",
        query: str = "",
        recursive: bool = False,
        max_results: int = 50,
    ) -> ToolExecResult:
        operation = str(operation or "").strip().lower()
        if operation not in _READ_ONLY_OPERATIONS:
            return (
                "error: unsupported Obsidian vault operation. "
                "Allowed operations: frontmatter, list, metadata, read, search."
            )

        try:
            wrapper = _build_wrapper(context)
            actor = _actor_from_context(context)
            if operation == "list":
                entries = wrapper.list(path, recursive=recursive, actor=actor)
                return _json_payload(
                    {
                        "operation": operation,
                        "entries": [asdict(entry) for entry in entries],
                    }
                )
            if operation == "read":
                content = wrapper.read(path, actor=actor)
                return _json_payload(
                    {"operation": operation, "path": path, "content": content}
                )
            if operation == "search":
                hits = wrapper.search(
                    query,
                    path,
                    actor=actor,
                    max_results=max(1, min(int(max_results), 200)),
                )
                return _json_payload(
                    {
                        "operation": operation,
                        "query": query,
                        "hits": [asdict(hit) for hit in hits],
                    }
                )
            if operation == "metadata":
                return _json_payload(
                    {
                        "operation": operation,
                        "metadata": wrapper.metadata(path, actor=actor),
                    }
                )
            return _json_payload(
                {
                    "operation": operation,
                    "frontmatter": wrapper.frontmatter(path, actor=actor),
                }
            )
        except (ValueError, PermissionError) as exc:
            return f"error: {exc}"
        except Exception as exc:  # noqa: BLE001
            detail = str(exc) or type(exc).__name__
            return f"error: Obsidian vault operation failed: {detail}"


__all__ = [
    "ObsidianVaultTool",
]
