from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot.core.tools import obsidian_vault_tools as tools
from astrbot.core.tools.obsidian_vault_tools import ObsidianVaultTool
from astrbot.core.tools.registry import get_builtin_tool_config_statuses


def _seed_vault(vault_root: Path) -> Path:
    notes = vault_root / "Notes"
    notes.mkdir(parents=True)
    note = notes / "Launch.md"
    note.write_text(
        "\n".join(
            [
                "---",
                "title: Launch SOP",
                "tags:",
                "  - launch",
                "---",
                "",
                "# Launch SOP",
                "",
                "Customer approval is required before launch.",
            ]
        ),
        encoding="utf-8",
    )
    return note


def _make_context(settings: dict) -> SimpleNamespace:
    config = {
        "provider_settings": {
            "obsidian_vault_automation": settings,
        }
    }
    return SimpleNamespace(
        context=SimpleNamespace(
            event=SimpleNamespace(
                unified_msg_origin="qq:friend:user-1",
                role="admin",
                get_sender_id=lambda: "user-1",
            ),
            context=SimpleNamespace(
                get_config=lambda umo: config,
            ),
        )
    )


def test_obsidian_vault_tool_is_registered_as_gated_builtin_tool() -> None:
    manager = FunctionToolManager()

    tool = manager.get_builtin_tool(ObsidianVaultTool)
    statuses = get_builtin_tool_config_statuses(
        "astrbot_obsidian_vault",
        [
            {
                "conf_id": "enabled",
                "conf_name": "enabled",
                "config": {
                    "provider_settings": {
                        "obsidian_vault_automation": {"enabled": True}
                    }
                },
            },
            {
                "conf_id": "disabled",
                "conf_name": "disabled",
                "config": {
                    "provider_settings": {
                        "obsidian_vault_automation": {"enabled": False}
                    }
                },
            },
        ],
    )

    assert tool.name == "astrbot_obsidian_vault"
    assert manager.is_builtin_tool("astrbot_obsidian_vault") is True
    assert statuses[0]["enabled"] is True
    assert statuses[1]["enabled"] is False


@pytest.mark.asyncio
async def test_obsidian_vault_tool_requires_configured_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "get_astrbot_data_path", lambda: str(tmp_path / "data"))
    tool = ObsidianVaultTool()

    result = await tool.call(
        _make_context({"enabled": True}),
        operation="list",
    )

    assert result == "error: No Obsidian vault roots are configured."


@pytest.mark.asyncio
async def test_obsidian_vault_tool_runs_read_only_operations(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "get_astrbot_data_path", lambda: str(tmp_path / "data"))
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    _seed_vault(vault_root)
    audit_log = tmp_path / "audit.jsonl"
    context = _make_context(
        {
            "enabled": True,
            "vault_roots": [str(vault_root)],
            "audit_log_path": str(audit_log),
        }
    )
    tool = ObsidianVaultTool()

    listed = json.loads(await tool.call(context, operation="list", path="Notes"))
    read = json.loads(
        await tool.call(context, operation="read", path="Notes/Launch.md")
    )
    searched = json.loads(
        await tool.call(context, operation="search", query="approval")
    )
    metadata = json.loads(
        await tool.call(context, operation="metadata", path="Notes/Launch.md")
    )
    frontmatter = json.loads(
        await tool.call(context, operation="frontmatter", path="Notes/Launch.md")
    )

    assert listed["entries"][0]["path"] == "Notes/Launch.md"
    assert "Customer approval is required" in read["content"]
    assert searched["hits"][0]["path"] == "Notes/Launch.md"
    assert metadata["metadata"]["sha256"]
    assert frontmatter["frontmatter"]["title"] == "Launch SOP"
    audit_rows = [
        json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["operation"] for row in audit_rows] == [
        "list",
        "read",
        "search",
        "metadata",
        "read",
        "frontmatter",
    ]
    assert {row["actor"] for row in audit_rows} == {"astrbot:admin:user-1"}


@pytest.mark.asyncio
async def test_obsidian_vault_tool_rejects_non_read_only_operations(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(tools, "get_astrbot_data_path", lambda: str(tmp_path / "data"))
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    _seed_vault(vault_root)
    tool = ObsidianVaultTool()
    context = _make_context(
        {
            "enabled": True,
            "vault_roots": [str(vault_root)],
        }
    )

    for operation in (
        "plan_write",
        "execute_write_plan",
        "write",
        "delete",
        "shell",
        "obsidian_cli",
        "defuddle",
    ):
        result = await tool.call(context, operation=operation)

        assert result.startswith("error: unsupported Obsidian vault operation")
