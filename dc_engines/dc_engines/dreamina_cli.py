"""Helpers for locating the Dreamina CLI in service environments."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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


def build_dreamina_subprocess_env(
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an environment for Dreamina CLI subprocesses.

    Args:
        env: Base environment to copy. Defaults to the current process
            environment.

    Returns:
        A subprocess environment that preserves existing proxy variables and,
        on macOS, mirrors the system proxy settings when needed.
    """
    effective_env = dict(os.environ if env is None else env)
    dreamina_http_proxy = effective_env.get(
        "DREAMINA_CLI_HTTP_PROXY"
    ) or effective_env.get("DREAMINA_CLI_PROXY")
    dreamina_https_proxy = (
        effective_env.get("DREAMINA_CLI_HTTPS_PROXY") or dreamina_http_proxy
    )
    dreamina_all_proxy = effective_env.get("DREAMINA_CLI_ALL_PROXY")
    if dreamina_http_proxy or dreamina_https_proxy or dreamina_all_proxy:
        if dreamina_http_proxy:
            effective_env["HTTP_PROXY"] = dreamina_http_proxy
            effective_env["http_proxy"] = dreamina_http_proxy
        if dreamina_https_proxy:
            effective_env["HTTPS_PROXY"] = dreamina_https_proxy
            effective_env["https_proxy"] = dreamina_https_proxy
        if dreamina_all_proxy:
            effective_env["ALL_PROXY"] = dreamina_all_proxy
            effective_env["all_proxy"] = dreamina_all_proxy
        return effective_env

    proxy_keys = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    if any(effective_env.get(key) for key in proxy_keys) or sys.platform != "darwin":
        return effective_env

    scutil_path = "/usr/sbin/scutil" if Path("/usr/sbin/scutil").exists() else "scutil"
    try:
        result = subprocess.run(
            [scutil_path, "--proxy"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return effective_env
    if result.returncode != 0 or not result.stdout:
        return effective_env

    proxy_config: dict[str, str] = {}
    exceptions: list[str] = []
    in_exceptions = False
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("ExceptionsList"):
            in_exceptions = True
            continue
        if in_exceptions:
            if line == "}":
                in_exceptions = False
                continue
            if " : " in line:
                _, value = line.split(" : ", 1)
                value = value.strip()
                if value and value != "<local>":
                    exceptions.append(value.replace("*.", "."))
            continue
        if " : " in line:
            key, value = line.split(" : ", 1)
            proxy_config[key.strip()] = value.strip()

    if proxy_config.get("HTTPEnable") == "1":
        host = proxy_config.get("HTTPProxy")
        port = proxy_config.get("HTTPPort")
        if host and port:
            effective_env.setdefault("HTTP_PROXY", f"http://{host}:{port}")
            effective_env.setdefault("http_proxy", f"http://{host}:{port}")
    if proxy_config.get("HTTPSEnable") == "1":
        host = proxy_config.get("HTTPSProxy")
        port = proxy_config.get("HTTPSPort")
        if host and port:
            effective_env.setdefault("HTTPS_PROXY", f"http://{host}:{port}")
            effective_env.setdefault("https_proxy", f"http://{host}:{port}")
    if proxy_config.get("SOCKSEnable") == "1":
        host = proxy_config.get("SOCKSProxy")
        port = proxy_config.get("SOCKSPort")
        if host and port:
            effective_env.setdefault("ALL_PROXY", f"socks5://{host}:{port}")
            effective_env.setdefault("all_proxy", f"socks5://{host}:{port}")
    if exceptions:
        effective_env.setdefault("NO_PROXY", ",".join(exceptions))
        effective_env.setdefault("no_proxy", ",".join(exceptions))
    return effective_env
