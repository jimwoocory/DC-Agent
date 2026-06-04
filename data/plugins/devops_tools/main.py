"""DevOps tools 给 LLM 调用的 function tools。

设计原则：
- 每个工具职责单一、return 结构化文字给 LLM（不直接发用户）
- LLM 拿到工具数据后整合回答，比工具直接发原始日志更友好
- 长输出截断到合理长度（避免 LLM context 爆炸）
- 敏感信息（key / token）做脱敏
- 只读，不做任何写操作（避免 LLM 误操作生产环境）

⚠️ 重要：工具必须 return string（不能 yield + 不 return）。
yield event.plain_result + 无 return → AstrBot 把 agent loop 标 DONE，
LLM 拿到 "tool has no return value" 就直接结束，永远不出答案。
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

DC_ROOT = Path("/Users/dianchi/DC-Agent")
WATCHDOG_STATE = DC_ROOT / "data" / "watchdog" / "state.json"
INCIDENT_DIR = DC_ROOT / "data" / "watchdog" / "incidents"

# 允许 LLM 读的 log 白名单（防止它 grep 敏感文件）
LOG_WHITELIST = {
    "astrbot": DC_ROOT / "astrbot.log",
    "astrbot-err": DC_ROOT / "astrbot.err.log",
    "hermes-gateway": DC_ROOT / "hermes-config" / "logs" / "gateway.log",
    "hermes-gateway-err": DC_ROOT / "hermes-config" / "logs" / "gateway.error.log",
    "feishu-sync": DC_ROOT / "nas_sync" / "feishu_sync.log",
    "nas-watchdog": DC_ROOT / "nas_sync" / "watchdog.log",
}

MAX_OUTPUT_CHARS = 4000  # 单次工具输出最大字符数（防 context 爆炸）


def _redact(text: str) -> str:
    """脱敏：把 key / token / secret 类字符串遮掉中间部分。"""
    text = re.sub(
        r"(sk-[A-Za-z0-9]{4})[A-Za-z0-9]{20,}([A-Za-z0-9]{4})", r"\1***\2", text
    )
    text = re.sub(
        r"(nvapi-[A-Za-z0-9]{4})[A-Za-z0-9_-]{20,}([A-Za-z0-9_-]{4})", r"\1***\2", text
    )
    text = re.sub(r"\"app_secret\"\s*:\s*\"[^\"]+\"", '"app_secret":"***"', text)
    return text


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [输出截断 · 共 {len(text)} 字符，仅显示前 {limit}]"


@register(
    "devops_tools",
    "dc_agent",
    "DevOps Console 的工具集 · 让 LLM 能查日志/watchdog/incident/git/launchctl",
    "1.0.0",
)
class DevOpsToolsPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)

    # ─────────────────────────── log 读取 ───────────────────────────

    @filter.llm_tool(name="read_log_tail")
    async def tool_read_log(
        self,
        event: AstrMessageEvent,
        name: str,
        lines: int = 50,
    ) -> str:
        """读取指定 log 文件的尾部内容。当用户问"看看 log"、"有没有报错"、"hermes 怎么样了"等时调用。

        Args:
            name(string): log 名称，可选: astrbot, astrbot-err, hermes-gateway, hermes-gateway-err, feishu-sync, nas-watchdog
            lines(number): 读取尾部多少行，默认 50，最大 300
        """
        lines = max(1, min(int(lines or 50), 300))
        log_path = LOG_WHITELIST.get(name)
        if log_path is None:
            return f"❌ 未知 log 名 `{name}`。可选: {', '.join(LOG_WHITELIST.keys())}"
        if not log_path.exists():
            return f"❌ log 文件不存在: {log_path}"
        try:
            result = await asyncio.to_thread(
                subprocess.check_output,
                ["tail", "-n", str(lines), str(log_path)],
                stderr=subprocess.STDOUT,
                timeout=10,
            )
            content = result.decode("utf-8", errors="ignore")
            content = _redact(content)
            content = _truncate(content)
            return f"📄 {name} (尾部 {lines} 行):\n```\n{content}\n```"
        except Exception as exc:  # noqa: BLE001
            return f"❌ 读取失败: {exc}"

    # ─────────────────────────── 看门狗状态 ───────────────────────────

    @filter.llm_tool(name="watchdog_state")
    async def tool_watchdog_state(self, event: AstrMessageEvent) -> str:
        """查看 dc-watchdog 当前 11 项探针的状态。当用户问"系统正常吗"、"看门狗怎么样"、"有没有告警"时调用。

        Args:
        """
        if not WATCHDOG_STATE.exists():
            return "❌ watchdog state.json 不存在"
        try:
            d = json.loads(WATCHDOG_STATE.read_text(encoding="utf-8"))
            lines = []
            ok_count = fail_count = 0
            for name, v in d.items():
                if not isinstance(v, dict):
                    continue
                status = v.get("status", "?")
                since = v.get("since", "?")
                if status == "ok":
                    ok_count += 1
                    lines.append(f"  ✅ {name}")
                else:
                    fail_count += 1
                    lines.append(f"  ❌ {name}  status={status}  since={since}")
            summary = f"探针 {ok_count}/{ok_count + fail_count} ok"
            body = "\n".join(lines)
            return f"🐶 dc-watchdog state · {summary}\n{body}"
        except Exception as exc:  # noqa: BLE001
            return f"❌ 解析 state.json 失败: {exc}"

    # ─────────────────────────── 最近 incident ───────────────────────────

    @filter.llm_tool(name="latest_incident")
    async def tool_latest_incident(self, event: AstrMessageEvent) -> str:
        """读取最近一份 codex 自动诊断的 incident 报告。当用户问"上次故障是什么"、"看下最近的告警"时调用。

        Args:
        """
        if not INCIDENT_DIR.exists():
            return "❌ incident 目录不存在"
        reports = sorted(
            INCIDENT_DIR.glob("incident-*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not reports:
            jsons = sorted(
                INCIDENT_DIR.glob("incident-*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not jsons:
                return "📭 还没有任何 incident（系统一直稳定）"
            content = _redact(jsons[0].read_text(encoding="utf-8"))
            return (
                f"📋 最近 incident snapshot ({jsons[0].name}):\n"
                f"```json\n{_truncate(content)}\n```"
            )
        latest = reports[0]
        content = _redact(latest.read_text(encoding="utf-8"))
        return f"📋 最近 codex 诊断报告 ({latest.name}):\n{_truncate(content)}"

    # ─────────────────────────── git 最近 commit ───────────────────────────

    @filter.llm_tool(name="git_recent_commits")
    async def tool_git_recent(
        self,
        event: AstrMessageEvent,
        count: int = 10,
    ) -> str:
        """看 git 最近 N 个 commit。当用户问"最近改了什么"、"项目最近的变更"时调用。

        Args:
            count(number): 显示多少个 commit，默认 10，最大 30
        """
        count = max(1, min(int(count or 10), 30))
        try:
            result = await asyncio.to_thread(
                subprocess.check_output,
                [
                    "git",
                    "-C",
                    str(DC_ROOT),
                    "log",
                    f"-{count}",
                    "--oneline",
                    "--decorate",
                ],
                stderr=subprocess.STDOUT,
                timeout=10,
            )
            content = result.decode("utf-8", errors="ignore")
            return f"📜 git 最近 {count} 个 commit:\n```\n{_truncate(content)}\n```"
        except Exception as exc:  # noqa: BLE001
            return f"❌ git log 失败: {exc}"

    # ─────────────────────────── launchd 服务状态 ───────────────────────────

    @filter.llm_tool(name="service_status")
    async def tool_service_status(
        self,
        event: AstrMessageEvent,
        label: str,
    ) -> str:
        """查 launchd 服务的运行状态。当用户问"AstrBot 还在跑吗"、"hermes 状态"等时调用。

        Args:
            label(string): launchd label，常用: io.astrbot.bot, ai.hermes.gateway, ai.hermes.dashboard, com.dcagent.feishu-sync, com.dcagent.nas-watchdog
        """
        allowed_prefixes = ("io.astrbot.", "ai.hermes.", "com.dcagent.")
        if not any(label.startswith(p) for p in allowed_prefixes):
            return (
                f"❌ 不允许查 `{label}` —— 只能查 DC-Agent 相关服务"
                f"（前缀: {allowed_prefixes}）"
            )
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["launchctl", "list", label],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return f"❌ `{label}` 不在 launchctl 列表 —— 可能没注册或没启动"
            content = _redact(result.stdout)
            return f"⚙️ {label}:\n```\n{_truncate(content)}\n```"
        except Exception as exc:  # noqa: BLE001
            return f"❌ launchctl list 失败: {exc}"
