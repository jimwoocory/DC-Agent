from __future__ import annotations

from pathlib import Path

from dc_engines.memory_governance.runtime_config import (
    build_content_sop_memory_governance_kwargs,
)


def test_content_sop_memory_governance_kwargs_use_explicit_plugin_config(
    tmp_path: Path,
) -> None:
    kwargs = build_content_sop_memory_governance_kwargs(
        tmp_path,
        {
            "data_dir": str(tmp_path / "plugin-data"),
            "obsidian_vault_path": str(tmp_path / "plugin-vault"),
        },
    )

    assert str(kwargs["memory_governance_store"].db_path) == str(  # type: ignore[attr-defined]
        tmp_path / "plugin-data" / "governed_memory.db"
    )
    assert kwargs["obsidian_vault_path"] == tmp_path / "plugin-vault"
