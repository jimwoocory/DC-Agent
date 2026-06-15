"""系统入口 plugin —— 把 Hermes Agent 官方 WebUI / OpenClaw / 其他系统服务的
快捷入口集中在 AstrBot dashboard 左侧 plugin 菜单里，**跟 dashboard 升级解耦**。

背景：以前在 dashboard 右上角加 "Hermes Agent 官方 WebUI" / "OpenClaw" 按钮是改 dashboard
JS bundle 实现，AstrBot 自带 dashboard 升级会被覆盖（5/15 v4.24.5 升级时发生过）。
这个 plugin 把入口做成"plugin page"——dashboard 是 v4.24.5 自带版本也好，
未来再升级也好，plugin page 始终在。

提供的能力：
1. plugin page: dashboard 左侧 plugin 菜单 → "系统入口" → 大按钮 + 在线指示灯
2. /api/plug/system_entries/status：返各服务状态 JSON（前端轮询）
3. /api/plug/system_entries/health：plugin 自身健康检查（看门狗用）
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from astrbot.api import logger
from astrbot.api.star import Context, Star, register

DEFAULT_ENTRIES: list[dict] = [
    {
        "name": "Hermes Agent 官方 WebUI",
        "url": "http://localhost:9119/",
        "probe_host": "127.0.0.1",
        "probe_port": 9119,
        "health_path": "/",
        "hint": "Hermes Agent 官方 UI / sessions 列表",
        "icon": "🤖",
    },
    {
        "name": "Hermes Agent 第三方 WebUI",
        "url": "http://localhost:8787/",
        "probe_host": "127.0.0.1",
        "probe_port": 8787,
        "health_path": "/",
        "hint": "EKKOLearnAI/hermes-web-ui 第三方界面",
        "icon": "💬",
    },
    {
        "name": "OpenClaw",
        "url": "http://localhost:4312/",
        "probe_host": "127.0.0.1",
        "probe_port": 4312,
        "health_path": "/",
        "hint": "按需启动 / 看门狗 :9120/kick",
        "icon": "🖥️",
        "on_demand_kick": "http://localhost:9120/kick",
    },
    {
        "name": "Hermes Gateway",
        "url": None,
        "probe_host": "127.0.0.1",
        "probe_port": 8644,
        "hint": "Hermes webhook 网关（后端，无 UI）",
        "icon": "🔌",
    },
    {
        "name": "AstrBot Response 通道",
        "url": None,
        "probe_host": "127.0.0.1",
        "probe_port": 8645,
        "hint": "Hermes → AstrBot 回调端口",
        "icon": "🔁",
    },
]

PINNED_DASHBOARD_ENTRIES: list[dict] = [
    {
        "name": "员工需求洞察",
        "url": "/#/employee-insight",
        "probe_host": None,
        "probe_port": None,
        "hint": "飞书私聊灰度验证、触达计划和员工需求闭环看板",
        "icon": "🧭",
        "pinned": True,
    },
    {
        "name": "记忆治理",
        "url": "/#/memory-governance",
        "probe_host": None,
        "probe_port": None,
        "hint": "NAS / Obsidian 记忆治理看板",
        "icon": "🧠",
        "pinned": True,
    },
    {
        "name": "内容 SOP 运营",
        "url": "/#/content-sop-ops",
        "probe_host": None,
        "probe_port": None,
        "hint": "内容 SOP 规则、记忆和审计运营看板",
        "icon": "📋",
        "pinned": True,
    },
]


@register(
    "system_entries",
    "dc_agent",
    "系统入口 — Hermes Agent 官方 WebUI / OpenClaw / 看门狗 等运维服务的快捷入口（跟 dashboard 升级解耦）",
    "1.0.0",
)
class SystemEntriesPlugin(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        cfg = config or {}
        self.entries: list[dict] = _merge_entries(
            cfg.get("entries") or DEFAULT_ENTRIES,
            PINNED_DASHBOARD_ENTRIES,
        )
        self.cache_ttl_seconds: int = int(cfg.get("cache_ttl_seconds", 5))
        self.watchdogctl_path: Path = Path(
            cfg.get(
                "watchdogctl_path",
                "/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh",
            )
        )
        self._cache: dict | None = None
        self._cache_at: float = 0.0

    async def initialize(self) -> None:
        try:
            self.context.register_web_api(
                "/system_entries/status",
                self._api_status,
                ["GET"],
                "各系统入口的在线状态 + 元数据",
            )
            self.context.register_web_api(
                "/system_entries/health",
                self._api_health,
                ["GET"],
                "plugin 自身健康检查（看门狗用）",
            )
            self.context.register_web_api(
                "/system_entries/watchdog",
                self._api_watchdog,
                ["GET"],
                "watchdog 控制台状态与人工 pause/resume",
            )
            logger.info(
                "[system_entries] API 已注册：/api/plug/system_entries/{status,health,watchdog}"
                "；plugin page 路径：/api/plugin/page/content/system_entries/dashboard/"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[system_entries] 注册 API 失败：%s", exc)

    # ────────────────────── API handlers ──────────────────────

    async def _api_health(self, *args, **kwargs):
        return {
            "status": "ok",
            "message": None,
            "data": {"plugin": "system_entries", "version": "1.0.0"},
        }

    async def _api_status(self, *args, **kwargs):
        """返每个 entry 的 alive 状态，5s 内 cache 防 hammer。"""
        now = time.time()
        if self._cache and now - self._cache_at < self.cache_ttl_seconds:
            return self._cache

        # Run probes concurrently; TCP alone is not service readiness.
        async def _probe(e):
            host = e.get("probe_host") or "127.0.0.1"
            port = int(e.get("probe_port") or 0)
            if not port:
                return {**e, "alive": None, "availability": "unknown"}
            alive = await asyncio.get_event_loop().run_in_executor(
                None, _tcp_alive, host, port
            )
            if not alive:
                return {
                    **e,
                    "alive": False,
                    "tcp_listening": False,
                    "availability": "offline",
                }
            health_url = _entry_health_url(e, host, port)
            if not health_url:
                return {
                    **e,
                    "alive": True,
                    "tcp_listening": True,
                    "availability": "tcp_listening",
                }
            health_ready = await asyncio.get_event_loop().run_in_executor(
                None, _http_health_ready, health_url, e
            )
            return {
                **e,
                "alive": health_ready,
                "tcp_listening": True,
                "health_url": health_url,
                "health_ok": health_ready,
                "availability": "ready" if health_ready else "wrong_service",
            }

        entries_with_status = await asyncio.gather(*(_probe(e) for e in self.entries))

        self._cache = {
            "status": "ok",
            "message": None,
            "data": {
                "entries": entries_with_status,
                "updated_at_unix": now,
            },
        }
        self._cache_at = now
        return self._cache

    async def _api_watchdog(self, *args, **kwargs):
        """Return or mutate watchdogctl state.

        GET query:
          action=status|pause|resume
          group=nas|night|sync|watchdog|dianchi-tech|onboarding|all
          target_type=launchd|cron|codex
          target_key=<task key>
        """
        action = str(kwargs.get("action") or "")
        group = str(kwargs.get("group") or "")
        target_type = str(kwargs.get("target_type") or "")
        target_key = str(kwargs.get("target_key") or "")

        if (not action or not group) and args:
            query = _extract_query(args[0])
            action = action or query.get("action", [""])[0]
            group = group or query.get("group", [""])[0]
            target_type = target_type or query.get("target_type", [""])[0]
            target_key = target_key or query.get("target_key", [""])[0]

        action = action or "status"
        group = group or "nas"
        allowed_actions = {"status", "pause", "resume"}
        allowed_target_types = {"", "launchd", "cron", "codex"}
        allowed_groups = {
            "all",
            "night",
            "nas",
            "sync",
            "watchdog",
            "dianchi-tech",
            "onboarding",
        }
        if action not in allowed_actions:
            return {"status": "error", "message": "invalid action", "data": None}
        if group not in allowed_groups:
            return {"status": "error", "message": "invalid group", "data": None}
        if target_type not in allowed_target_types:
            return {"status": "error", "message": "invalid target type", "data": None}
        if target_type and not target_key:
            return {"status": "error", "message": "missing target key", "data": None}
        if not self.watchdogctl_path.exists():
            return {
                "status": "error",
                "message": f"watchdogctl missing: {self.watchdogctl_path}",
                "data": None,
            }

        def _run_watchdogctl() -> dict:
            if action in {"pause", "resume"}:
                if target_type:
                    command = "pause-one" if action == "pause" else "resume-one"
                    first_args = [
                        str(self.watchdogctl_path),
                        command,
                        target_type,
                        target_key,
                    ]
                else:
                    first_args = [str(self.watchdogctl_path), action, group]
                first = subprocess.run(
                    first_args,
                    text=True,
                    capture_output=True,
                    timeout=20,
                )
                if first.returncode != 0:
                    return {
                        "ok": False,
                        "stdout": first.stdout,
                        "stderr": first.stderr,
                        "returncode": first.returncode,
                    }
            second = subprocess.run(
                [str(self.watchdogctl_path), "status", group, "--json"],
                text=True,
                capture_output=True,
                timeout=20,
            )
            if second.returncode != 0:
                return {
                    "ok": False,
                    "stdout": second.stdout,
                    "stderr": second.stderr,
                    "returncode": second.returncode,
                }
            return {
                "ok": True,
                "state": json.loads(second.stdout),
                "stdout": second.stdout,
                "stderr": second.stderr,
                "returncode": second.returncode,
            }

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, _run_watchdogctl
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[system_entries] watchdogctl 调用失败：%s", exc)
            return {"status": "error", "message": str(exc), "data": None}

        if not result.get("ok"):
            return {
                "status": "error",
                "message": result.get("stderr") or result.get("stdout") or "failed",
                "data": result,
            }
        return {
            "status": "ok",
            "message": None,
            "data": {
                "action": action,
                "group": group,
                "state": result["state"],
            },
        }


def _tcp_alive(host: str, port: int) -> bool:
    if not port:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect((host, port))
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


def _entry_health_url(entry: dict, host: str, port: int) -> str | None:
    health_url = str(entry.get("health_url") or "").strip()
    if health_url:
        return health_url
    health_path = str(entry.get("health_path") or "").strip()
    if not health_path:
        return None
    if not health_path.startswith("/"):
        health_path = f"/{health_path}"
    scheme = str(entry.get("health_scheme") or "http").strip() or "http"
    return f"{scheme}://{host}:{port}{health_path}"


def _http_health_ready(url: str, entry: dict) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme != "http" or not parsed.hostname:
            return False
        port = int(parsed.port or 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        with socket.create_connection((parsed.hostname, port), timeout=0.75) as sock:
            sock.settimeout(0.75)
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {parsed.hostname}:{port}\r\n"
                "User-Agent: DC-Agent-system-entries-health\r\n"
                "Connection: close\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
            payload = _recv_http_response(sock)
        header, _, body = payload.partition(b"\r\n\r\n")
        status_line = header.splitlines()[0].decode("ascii", errors="ignore")
        parts = status_line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            return False
        if not _health_status_matches(int(parts[1]), entry):
            return False
        body_contains = str(entry.get("health_body_contains") or "")
        if body_contains:
            return body_contains in body.decode("utf-8", errors="ignore")
        return True
    except Exception:
        return False


def _recv_http_response(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < 65536:
        chunk = sock.recv(min(4096, 65536 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _health_status_matches(status: int, entry: dict) -> bool:
    expected = entry.get("health_status")
    if expected is None:
        expected = entry.get("health_status_code")
    if expected is None:
        expected = entry.get("health_status_codes")
    if expected is None:
        return 200 <= status < 400
    if isinstance(expected, list):
        return status in {int(item) for item in expected}
    return status == int(expected)


def _merge_entries(entries: list[dict], pinned_entries: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for entry in [*entries, *pinned_entries]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        merged.append(entry)
    return merged


def _extract_query(request) -> dict[str, list[str]]:
    rel_url = getattr(request, "rel_url", None)
    query_string = getattr(rel_url, "query_string", "") if rel_url is not None else ""
    if not query_string:
        query_string = getattr(request, "query_string", "")
    return parse_qs(query_string)
