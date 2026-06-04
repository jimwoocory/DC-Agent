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
from urllib.parse import parse_qs

from astrbot.api import logger
from astrbot.api.star import Context, Star, register


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
        # 默认 4 个入口；通过 _conf_schema.json 可定制
        self.entries: list[dict] = cfg.get("entries") or [
            {
                "name": "Hermes Agent 官方 WebUI",
                "url": "http://localhost:9119/",
                "probe_host": "127.0.0.1",
                "probe_port": 9119,
                "hint": "Hermes Agent 官方 UI / sessions 列表",
                "icon": "🤖",
            },
            {
                "name": "Hermes Agent 第三方 WebUI",
                "url": "http://localhost:8787/",
                "probe_host": "127.0.0.1",
                "probe_port": 8787,
                "hint": "EKKOLearnAI/hermes-web-ui 第三方界面",
                "icon": "💬",
            },
            {
                "name": "OpenClaw",
                "url": "http://localhost:4312/",
                "probe_host": "127.0.0.1",
                "probe_port": 4312,
                "hint": "按需启动 / 看门狗 :9120/kick",
                "icon": "🖥️",
                "on_demand_kick": "http://localhost:9120/kick",
            },
            {
                "name": "Hermes Gateway",
                "url": None,  # 后端服务，无 UI
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

        # 并发探活各端口（TCP connect，0.5s 超时）
        async def _probe(e):
            host = e.get("probe_host") or "127.0.0.1"
            port = int(e.get("probe_port") or 0)
            if not port:
                return {**e, "alive": None}
            alive = await asyncio.get_event_loop().run_in_executor(
                None, _tcp_alive, host, port
            )
            return {**e, "alive": alive}

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


def _extract_query(request) -> dict[str, list[str]]:
    rel_url = getattr(request, "rel_url", None)
    query_string = getattr(rel_url, "query_string", "") if rel_url is not None else ""
    if not query_string:
        query_string = getattr(request, "query_string", "")
    return parse_qs(query_string)
