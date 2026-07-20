from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


class _AdminEvent:
    def get_sender_id(self) -> str:
        return "admin-user"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "ObsidianVault"
    (vault / "10_Index").mkdir(parents=True)
    (vault / "20_Bridges").mkdir()
    (vault / "10_Index" / "公司知识地图.md").write_text(
        "# 公司知识地图\n\n五菱项目由市场部负责。\n",
        encoding="utf-8",
    )
    (vault / "20_Bridges" / "项目总表.md").write_text(
        "# 项目总表\n\n关联 [[10_Index/公司知识地图]]。\n",
        encoding="utf-8",
    )
    return vault


def _plugin(tmp_path: Path):
    from data.plugins.obsidian_vault_tools.main import ObsidianVaultToolsPlugin

    context = SimpleNamespace(get_config=lambda: {"admins_id": ["admin-user"]})
    plugin = object.__new__(ObsidianVaultToolsPlugin)
    plugin.context = context
    plugin._config = {"obsidian_vault_path": str(_vault(tmp_path))}
    return plugin


@pytest.mark.asyncio
async def test_agent_can_search_read_and_list_real_vault_notes(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    event = _AdminEvent()

    search = json.loads(await plugin.search_obsidian_vault(event, "五菱", ".", 10))
    assert search["status"] == "ok"
    assert search["hits"][0]["path"] == "10_Index/公司知识地图.md"
    assert search["hits"][0]["line_number"] == 3

    note = json.loads(
        await plugin.read_obsidian_note(
            event,
            "10_Index/公司知识地图.md",
            8_000,
        )
    )
    assert note["status"] == "ok"
    assert note["source"] == "ObsidianVault/10_Index/公司知识地图.md"
    assert "五菱项目" in note["content"]

    listing = json.loads(await plugin.list_obsidian_vault(event, ".", 50))
    assert listing["status"] == "ok"
    assert [entry["path"] for entry in listing["entries"]] == [
        "10_Index",
        "20_Bridges",
    ]


@pytest.mark.asyncio
async def test_obsidian_tools_reject_non_admin_and_path_escape(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    denied_event = SimpleNamespace(get_sender_id=lambda: "employee-user")
    (tmp_path / "outside.md").write_text("outside", encoding="utf-8")

    denied = json.loads(
        await plugin.search_obsidian_vault(denied_event, "五菱", ".", 10)
    )
    assert denied == {"status": "forbidden", "reason": "admin_only"}

    escaped = json.loads(
        await plugin.read_obsidian_note(_AdminEvent(), "../outside.md", 8_000)
    )
    assert escaped["status"] == "error"
    assert "escapes configured root" in escaped["reason"]


def test_nas_compose_mounts_company_vault_read_only() -> None:
    compose = Path("deploy/nas-unified/compose.yml").read_text(encoding="utf-8")

    assert "DC_OBSIDIAN_VAULT_PATH: /AstrBot/ObsidianVault" in compose
    assert (
        "/volume1/knowledge/dc-agent-obsidian/ObsidianVault:/AstrBot/ObsidianVault:ro"
    ) in compose
