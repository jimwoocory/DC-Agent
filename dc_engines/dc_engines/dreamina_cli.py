"""Helpers for locating the Dreamina CLI in service environments."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_dreamina_executable(
    env: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> str | None:
    """Resolve the Dreamina CLI without relying only on the service PATH."""
    effective_env = os.environ if env is None else env
    override = str(effective_env.get("DREAMINA_CLI_PATH", "")).strip()
    if override:
        override_path = Path(override).expanduser()
        if _is_executable_file(override_path):
            return str(override_path)
        resolved_override = shutil.which(override, path=effective_env.get("PATH"))
        if resolved_override:
            return resolved_override

    resolved = shutil.which("dreamina", path=effective_env.get("PATH"))
    if resolved:
        return resolved

    home_dir = Path.home() if home is None else home
    candidates = (
        home_dir / ".local" / "bin" / "dreamina",
        home_dir / ".npm-global" / "bin" / "dreamina",
        Path("/opt/homebrew/bin/dreamina"),
        Path("/usr/local/bin/dreamina"),
    )
    for candidate in candidates:
        if _is_executable_file(candidate):
            return str(candidate)
    return None


def dreamina_command_not_found_message() -> str:
    default_path = Path.home() / ".local" / "bin" / "dreamina"
    return (
        "未找到 dreamina CLI。请确认已安装并登录，或设置 "
        f"DREAMINA_CLI_PATH={default_path} 后重启服务。"
    )
