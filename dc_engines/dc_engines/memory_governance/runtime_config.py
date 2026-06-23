from __future__ import annotations

from pathlib import Path
from typing import Any

from dc_engines.memory_governance.store import MemoryGovernanceStore


def build_content_sop_memory_governance_kwargs(
    dc_root: Path,
    config: dict[str, Any] | None,
) -> dict[str, object]:
    cfg = config or {}
    data_dir_override = str(cfg.get("data_dir") or "").strip()
    data_dir = Path(data_dir_override) if data_dir_override else dc_root / "data"
    vault_override = str(cfg.get("obsidian_vault_path") or "").strip()
    vault_path = Path(vault_override) if vault_override else dc_root / "ObsidianVault"
    return {
        "memory_governance_store": MemoryGovernanceStore(
            data_dir / "governed_memory.db"
        ),
        "obsidian_vault_path": vault_path,
    }
