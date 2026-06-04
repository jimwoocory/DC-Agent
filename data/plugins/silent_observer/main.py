"""Silent grey-test observer for the maintenance bot.

The observer records real user feedback during grey pressure tests and never
replies to the group. It is intentionally separated from the Hermes bridge so
the maintenance bot cannot affect the business bot's topic workflow.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path


@register(
    "silent_observer",
    "silent_observer",
    "灰度压力测试静默观察员，只记录不发言",
    version="1.0.0",
)
class SilentObserverPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        cfg = context.get_config()
        ocfg = cfg.get("silent_observer", {})
        self.enabled: bool = ocfg.get("enabled", True)
        self.observer_platforms: list[str] = ocfg.get(
            "observer_platforms",
            ["巅池-技术"],
        )
        self.ignore_self_messages: bool = ocfg.get("ignore_self_messages", True)
        self.max_message_chars: int = int(ocfg.get("max_message_chars", 2000))
        self.data_dir = Path(get_astrbot_plugin_data_path()) / "silent_observer"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    async def on_message(self, event: AstrMessageEvent) -> None:
        if not self.enabled:
            return

        platform_id = str(event.get_platform_id() or "")
        if platform_id not in self.observer_platforms:
            return

        event.stop_event()

        sender_id = str(event.get_sender_id() or "")
        if (
            self.ignore_self_messages
            and sender_id
            and sender_id == str(event.get_self_id() or "")
        ):
            return

        text = self._extract_text(event)
        if not text:
            return

        record = {
            "observed_at": self._now_iso(),
            "platform_id": platform_id,
            "platform_name": event.get_platform_name(),
            "session_id": event.unified_msg_origin,
            "group_id": event.get_group_id(),
            "sender_id": sender_id,
            "sender_name": event.get_sender_name() or sender_id,
            "message": text[: self.max_message_chars],
            "categories": self._classify_feedback(text),
            "is_group": event.is_group(),
        }
        await asyncio.to_thread(self._append_record, record)

    def _extract_text(self, event: AstrMessageEvent) -> str:
        parts = [
            str(component.text)
            for component in event.get_messages()
            if isinstance(component, Plain)
        ]
        if parts:
            return "".join(parts).strip()
        return (event.message_str or "").strip()

    def _append_record(self, record: dict) -> None:
        path = self.data_dir / f"{record['observed_at'][:10]}.jsonl"
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            file.write("\n")
        logger.debug(
            "[SilentObserver] 记录灰度反馈：platform=%s session=%s sender=%s",
            record["platform_id"],
            record["session_id"],
            record["sender_name"],
        )

    def _classify_feedback(self, text: str) -> list[str]:
        categories = []
        if self._contains_any(
            text,
            ("机器人", "bot", "推广 1 号", "推广1号", "Hermes", "系统", "模型"),
        ):
            categories.append("system_or_bot_feedback")
        if self._contains_any(
            text,
            ("慢", "卡", "延迟", "没反应", "超时", "失败", "报错", "断了"),
        ):
            categories.append("stability_or_latency")
        if self._contains_any(
            text,
            ("不满意", "不行", "不对", "不够", "不落地", "太空", "没解决"),
        ):
            categories.append("dissatisfaction")
        if self._contains_any(
            text,
            ("看不懂", "不会用", "指令", "点名", "群名片", "流程", "说明"),
        ):
            categories.append("process_usability")
        if self._contains_any(
            text,
            ("老板", "部门", "员工", "负责人", "角色", "权限"),
        ):
            categories.append("role_or_org_signal")
        if self._contains_any(
            text,
            ("知识库", "沉淀", "资料", "文档", "标准", "话术", "案例"),
        ):
            categories.append("knowledge_candidate")
        if not categories:
            categories.append("general_observation")
        return categories

    def _contains_any(self, text: str, keywords: tuple[str, ...]) -> bool:
        lowered = text.lower()
        return any(keyword.lower() in lowered for keyword in keywords)

    def _now_iso(self) -> str:
        return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
