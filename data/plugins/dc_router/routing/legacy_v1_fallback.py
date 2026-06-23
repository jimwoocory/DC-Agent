"""v1.0 legacy intent classification — only used when dc_router is disabled.

v1.0 路径由 3 层组成：
1. 前缀指令 → 员工显式 #深度 / #快速
2. LLM 意图识别 → 调小 LLM (Gemini Flash) 判断
3. 关键词兜底 → regex 匹配 (LLM 失败时)

这个模块 *只* 在 dc_router_config.enabled=false 时被 dispatch 调用；
正常生产路径 (enabled=true) 不进这里。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Final

from astrbot.api import logger

# 保留 5 个核心意图 (跟 dc_router_core/taxonomy 不完全一致 — v1.0 是「路由桶」,
# 真正的语义分类在 dc_router_core 那一层做)
V1_INTENTS: Final[tuple[str, ...]] = (
    "casual",
    "writing",
    "deep",
    "realtime",
    "code",
)

# v1.0 意图 → provider_id (这是 v1.0 路径 *唯一* 还允许的「意图→provider」映射)
V1_INTENT_TO_PROVIDER: Final[dict[str, str]] = {
    "casual": "aihubmix/qwen3.6-flash",
    "writing": "aihubmix/qwen3.6-flash",
    "deep": "aihubmix/claude-opus-4-7",
    "realtime": "aihubmix/grok-4.3",
    "code": "codex/gpt-5.4",
}

V1_PREFIX_INTENTS: Final[dict[str, str]] = {
    "#深度": "deep",
    "#deep": "deep",
    "#实时": "realtime",
    "#realtime": "realtime",
    "#快速": "casual",
    "#fast": "casual",
    "#写作": "writing",
    "#writing": "writing",
    "#代码": "code",
    "#code": "code",
}

ROUTER_LLM_PROVIDER_V1: Final[str] = "aihubmix/gemini-3.5-flash"

ROUTER_SYSTEM_PROMPT_V1: Final[str] = """你是 LLM 路由判断器。

任务: 看用户最近的一条消息，判断它属于哪一类，只输出 JSON。

类别定义（5 选 1）:
- casual: 日常寒暄、问候、简单问答、闲聊、表达情绪
- writing: 写文案/通知/汇报/周报/总结/纪要/海报文案/脚本/方案草稿/介绍稿/客户触达话术；邮件只在用户明确要求时使用
- deep: 深度分析/系统拆解/战略规划/复杂决策/根因诊断/完整研究报告/全面评估
- realtime: 实时舆情/竞品监控/热点追踪/最新动态/社交媒体趋势/今天/最近发生的事
- code: 代码相关/技术调试/工程问题/API/SDK 使用

输出格式（严格 JSON，无其他任何文字）:
{"intent": "casual|writing|deep|realtime|code"}
"""

_JSON_RE: Final[re.Pattern] = re.compile(r"\{[^{}]*\}", re.DOTALL)


# 关键词兜底
_KEYWORD_RULES_V1: Final[tuple[tuple[str, re.Pattern], ...]] = (
    (
        "realtime",
        re.compile(
            r"(最近|今天|现在|刚才|刚刚|当前|实时|这周|本周)?.*?(热点|舆情|竞品|趋势|爆款|刷屏|出圈|社交|微博|抖音)",
        ),
    ),
    (
        "deep",
        re.compile(
            r"(深度|彻底|详尽|完整|系统|战略|框架|结构化|根本|本质|核心|全方位|深入)"
            r".*?(分析|思考|拆解|梳理|规划|报告|策略|建议|审视|论证|评估|研究)",
        ),
    ),
    (
        "writing",
        re.compile(
            r"(写|起草|生成|做|帮我写|帮我做|帮我起草).{0,10}?(邮件|文案|文章|周报|月报|汇报|总结|纪要|海报|脚本|提案|方案|策划|说明|通知|公告|介绍|稿件|台词|对话|流程|清单|笔记)",
        ),
    ),
    (
        "code",
        re.compile(
            r"(代码|脚本|bug|错误|报错|debug|api|接口|sdk|python|javascript|java|sql)",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class V1Classification:
    intent: str
    source: str  # "prefix" | "llm" | "keyword"


def classify_intent_v1(
    text: str,
    *,
    dynamic_aliases: list[dict] | None = None,
) -> V1Classification | None:
    """按 prefix → keyword 顺序分类（v1.0 路径里 LLM 分类单独异步调用）。"""
    stripped = text.strip()

    for prefix, intent in V1_PREFIX_INTENTS.items():
        if stripped.lower().startswith(prefix.lower()):
            return V1Classification(intent=intent, source="prefix")

    if dynamic_aliases:
        for item in dynamic_aliases:
            if not isinstance(item, dict):
                continue
            intent = str(item.get("intent") or "")
            pattern = str(item.get("pattern") or "")
            if intent not in V1_INTENT_TO_PROVIDER or not pattern:
                continue
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    return V1Classification(intent=intent, source="keyword")
            except re.error as exc:
                logger.debug(
                    "[dc_router · v1] invalid alias pattern=%r: %s",
                    pattern,
                    exc,
                )

    for intent, pattern in _KEYWORD_RULES_V1:
        if pattern.search(text):
            return V1Classification(intent=intent, source="keyword")
    return None


async def reason_with_llm_v1(
    context: Any,
    text: str,
    *,
    timeout_sec: float = 20.0,
) -> str | None:
    """调小 LLM 判断意图。失败 / 超时返回 None，由调用方走 keyword 兜底。"""
    try:
        providers = {p.meta().id: p for p in context.get_all_providers()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc_router · v1] get_all_providers 失败: %s", exc)
        return None
    provider = providers.get(ROUTER_LLM_PROVIDER_V1)
    if provider is None:
        logger.debug("[dc_router · v1] router LLM %s 不存在", ROUTER_LLM_PROVIDER_V1)
        return None
    try:
        resp = await asyncio.wait_for(
            provider.text_chat(
                prompt=text[:500],
                system_prompt=ROUTER_SYSTEM_PROMPT_V1,
                contexts=[],
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.warning("[dc_router · v1] LLM 判定超时 (>%.1fs)", timeout_sec)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router · v1] LLM 判定失败: %s", exc)
        return None
    raw = (getattr(resp, "completion_text", "") or "").strip()
    match = _JSON_RE.search(raw)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    intent = str(data.get("intent", "")).strip().lower()
    if intent in V1_INTENT_TO_PROVIDER:
        return intent
    return None


__all__ = [
    "V1_INTENT_TO_PROVIDER",
    "V1_INTENTS",
    "V1Classification",
    "classify_intent_v1",
    "reason_with_llm_v1",
]
