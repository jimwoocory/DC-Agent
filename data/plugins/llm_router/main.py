# ruff: noqa: E402
"""LLM Router · 智能路由 v1.0

3 层决策（优先级从高到低）：
1. 前缀指令 · 员工显式指定: #深度 / #实时 / #快速 / #写作
2. LLM 意图识别 · 调小 LLM（Gemini Flash）判断
3. 关键词兜底 · regex 匹配（LLM 调用失败时）

切换机制：
- 用 AstrBot 的 set_provider(provider_id, umo=...) 按会话切
- 同一会话内对话有"连贯一致"特性（除非下条消息又匹配到别的意图）
- 不命中任何规则 → 不动（走全局默认）

仅在 巅池-Agent 小助手 启用（其他机器人不受影响）。

TODO 5/25 后:
- 提取到 dc_engines/router/ 引擎，让 Hermes / 未来前端共享
- 加路由日志 / 准确率统计
- 加 token 消耗 dashboard
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import random
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from dc_engines.card_runtime import finalize_card_via_runtime, send_card_via_runtime
from dc_engines.department_workflows.memory_profiles import (
    matching_department_memory_profiles,
)
from dc_engines.feishu_card_streamer import (
    WaitingCardHandle,
    build_media_generation_card,
    ensure_streamers_on_context,
    extract_chat_info_from_event,
    start_waiting_card_for_event,
)
from dc_engines.media_sop import (
    build_media_generation_record,
    build_structured_media_prompt,
)

UTC = timezone.utc

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Image, Plain, Video
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderType
from astrbot.core.runtime_context.memory_query import build_memory_retrieval_query

try:
    from dc_engines.assistant_distillation import load_language_overrides
except Exception:  # noqa: BLE001
    load_language_overrides = None

# ─────────────────── 配置 ───────────────────

# 意图 → 主模型 provider id
# 2026-05-20: 路由层统一绕开 Gemini CLI/OAuth 链路。
# 高频闲聊/写作走 aihubmix Qwen Flash；深度分析临时走 aihubmix Claude。
INTENT_TO_PROVIDER = {
    "casual": "aihubmix/qwen3.6-flash",  # 日常闲聊 · 不占用 agy
    "writing": "aihubmix/qwen3.6-flash",  # 写邮件/文案 · 不占用 agy
    "deep": "aihubmix/claude-opus-4-7",  # 深度分析 · 临时 Hermes 通道
    "realtime": "aihubmix/grok-4.3",  # 实时舆情/热点 · 无替代
    "code": "codex/gpt-5.4",  # 代码 · v1 兜底用 Codex provider
}

# 路由判断 LLM（aihubmix gemini-3.5-flash）
ROUTER_LLM_PROVIDER = "aihubmix/gemini-3.5-flash"

# Platform → 路由模式映射
# - "intent": 走 3 层意图判断（prefix → LLM → keyword）
# - 具体 provider_id（如 "codex/gpt-5.4"）: 该 platform 默认强 pin 到这个 provider
#   （但前缀指令如 #高 / #超深 会覆盖）
ENABLED_PLATFORMS: dict[str, str] = {
    "巅池-Agent小助手": "intent",
    "巅池-技术（DevOps）": "codex/gpt-5.4",  # dc-router 失败时的 DevOps v1 兜底
}

# 推理级别前缀（高优先级，命中后直接 pin 对应 provider）
# 默认走 AIHubMix Gemini；带 #codex 前缀强制走 codex 备用
REASONING_PREFIX_PROVIDERS: dict[str, str] = {
    # ── Gemini via AIHubMix（三档语义保留，provider 不再走 Gemini OAuth/CLI）──
    "#中": "aihubmix/gemini-3.5-flash",
    "#medium": "aihubmix/gemini-3.5-flash",
    "#fast": "aihubmix/gemini-3.5-flash",
    "#高": "aihubmix/claude-sonnet-4-6",
    "#high": "aihubmix/claude-sonnet-4-6",
    "#超深": "aihubmix/claude-opus-4-7",
    "#超高": "aihubmix/claude-opus-4-7",
    "#xhigh": "aihubmix/claude-opus-4-7",
    "#深度": "aihubmix/claude-opus-4-7",
    # ── Codex 三档（备用 / 跨模型对照）──
    "#codex": "codex/gpt-5.5-medium",
    "#codex高": "codex/gpt-5.5-high",
    "#codex超深": "codex/gpt-5.5-xhigh",
}

# 前缀指令映射（员工显式指定）
PREFIX_INTENTS = {
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

# 关键词兜底规则（LLM 路由失败时用）
_KEYWORD_RULES: list[tuple[str, re.Pattern]] = [
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
]

_CHITCHAT_MAX_LEN = 8
_CHITCHAT_RATE_LIMIT_SEC = 3.0
_CHITCHAT_MISS_LOG_PATH = Path(
    "/Users/dianchi/DC-Agent/data/chitchat_guard_misses.jsonl"
)
_CHITCHAT_HIT_LOG_PATH = Path("/Users/dianchi/DC-Agent/data/chitchat_guard_hits.jsonl")
_ASSISTANT_LANGUAGE_OVERRIDES_PATH = Path(
    "/Users/dianchi/DC-Agent/data/config/assistant_language_overrides.json"
)
_DEPARTMENT_MEMORY_PROMPT_AUDIT_PATH = Path(
    "/Users/dianchi/DC-Agent/data/department_memory_prompt_audit.jsonl"
)
_CHITCHAT_LAST_HIT: dict[str, float] = {}
_DEPARTMENT_MEMORY_PROMPT_TTL_SEC = 600.0
_PENDING_DEPARTMENT_MEMORY_PROMPTS: dict[str, _DepartmentMemoryPromptState] = {}
_CHITCHAT_PUNCT_RE = re.compile(
    r"[\s，。！？、~～?!\.,;；:：\"'“”‘’（）()【】\[\]{}<>《》]+"
)
_CHITCHAT_AT_RE = re.compile(r"^\s*(?:\[At:[^\]]+\]|@[^\s]+\s*)+")
_CHITCHAT_NEGATIVE_RE = re.compile(
    r"(查|调|写|改|跑|算|搜|找|做|生成|优化|报错|错误|bug|任务|待办|提醒|方案|项目|资料|文件|链接|推文|群)"
)
_BUSINESS_TONE_RE = re.compile(
    r"(视频|脚本|文案|分镜|拍摄|选题|传播|混剪|纪录片|采访|发布|转发|封面|"
    r"五菱|缤果|星光|MINIEV|mini|菱骏|红标|扬光|宝骏|柳汽|东风|风行|乘龙|菱智)",
    re.IGNORECASE,
)
_ASSISTANT_TONE_OPEN_MARKER = "<assistant_tone_context>"
_ASSISTANT_TONE_CLOSE_MARKER = "</assistant_tone_context>"
_DEPARTMENT_MEMORY_CONFIRM_RE = re.compile(
    r"^\s*(调用记忆|带上记忆|使用记忆|用记忆|确认调用|确认|可以|好的|好|是|yes|y|ok)\s*[。！!,.，]*\s*$",
    re.IGNORECASE,
)
_DEPARTMENT_MEMORY_DISMISS_RE = re.compile(
    r"^\s*(不用|不调用|不用记忆|先不用|不要|取消|否|no|n)\s*[。！!,.，]*\s*$",
    re.IGNORECASE,
)
_EXPLICIT_MEMORY_LOOKUP_RE = re.compile(
    r"(记忆|历史|之前|查一下|找一下|有没有|是谁|是什么|负责人|资料|文件|来源|引用)",
    re.IGNORECASE,
)
_CHITCHAT_RESPONSES: dict[str, dict[str, tuple[str, ...]]] = {
    "greeting": {
        "keywords": (
            "你好",
            "您好",
            "hello",
            "hi",
            "hey",
            "你好呀",
            "在吗",
            "在不",
            "在",
            "在？",
            "喂",
        ),
        "responses": (
            "您好，我在的。您需要我协助处理什么内容，直接发我就好。",
            "在的，您可以直接把需要我协助的内容发给我。",
            "您好，我在。需要我帮您看资料、整理内容或处理问题，都可以直接发我。",
        ),
    },
    "thanks": {
        "keywords": (
            "谢谢",
            "感谢",
            "谢了",
            "太感谢了",
            "辛苦了",
            "麻烦你了",
            "thanks",
            "thx",
        ),
        "responses": (
            "不客气，后续有需要您随时找我。",
            "不辛苦，能帮上忙就好。您后面有需要可以继续发我。",
            "收到，后续需要我继续协助的话，您直接说就好。",
        ),
    },
    "farewell": {
        "keywords": ("再见", "拜拜", "bye", "goodbye"),
        "responses": (
            "好的，后续有需要您随时找我。",
            "再见，祝您工作顺利。",
        ),
    },
    "identity": {
        "keywords": ("你是谁", "你叫啥", "你是啥", "你是什么", "你叫什么"),
        "responses": (
            "我是巅池-Agent 小助手，可以协助您整理资料、优化内容、查询项目和处理日常协作问题。",
            "我是巅池-Agent 小助手，主要协助大家做资料整理、内容优化、项目查询和工作协同。",
        ),
    },
}

# LLM 路由判断 system_prompt
ROUTER_SYSTEM_PROMPT = """你是 LLM 路由判断器。

任务: 看用户最近的一条消息，判断它属于哪一类，只输出 JSON。

类别定义（5 选 1）:
- casual: 日常寒暄、问候、简单问答、闲聊、表达情绪
- writing: 写文案/通知/汇报/周报/总结/纪要/海报文案/脚本/方案草稿/介绍稿/客户触达话术；邮件只在用户明确要求时使用
- deep: 深度分析/系统拆解/战略规划/复杂决策/根因诊断/完整研究报告/全面评估
- realtime: 实时舆情/竞品监控/热点追踪/最新动态/社交媒体趋势/今天/最近发生的事
- code: 代码相关/技术调试/工程问题/API/SDK 使用

输出格式（严格 JSON，无其他任何文字）:
{"intent": "casual|writing|deep|realtime|code"}

例:
用户: "你好" → {"intent": "casual"}
用户: "帮我写一段端午客户微信问候话术" → {"intent": "writing"}
用户: "深度分析竞品最近 3 个月动作" → {"intent": "deep"}
用户: "今天 X 行业热点是什么" → {"intent": "realtime"}
用户: "我的 Python 脚本报错" → {"intent": "code"}
"""

_JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


@dataclass(frozen=True, slots=True)
class _DepartmentMemoryPromptState:
    suggestion_id: str
    conversation_id: str
    original_text: str
    query_text: str
    department_ids: tuple[str, ...]
    department_names: tuple[str, ...]
    profile_ids: tuple[str, ...]
    created_at: float
    status: str = "suggested"


@dataclass(frozen=True, slots=True)
class _DepartmentMemoryPromptDecision:
    stop: bool = False
    inject_memory: bool = False
    dismissed: bool = False
    effective_text: str = ""
    memory_query_text: str = ""
    suggestion_id: str = ""
    audit_state: _DepartmentMemoryPromptState | None = None


# ─── dc-router 开关 ─────────────────────────────────────────────────────────
# 配置文件: data/config/dc_router_config.json
# enabled=false (默认) → 走以下 v1.0 逻辑（行为跟旧版完全一样）
# enabled=true        → 启用 dc-router 新路径（步骤 3 之后才真正接通）
# 默认状态由人手动改 JSON 控制；Claude 不会自动打开此开关（硬规则）。
import os

_DC_AGENT_ROOT = Path(__file__).resolve().parents[3]
_DC_ROUTER_CONFIG_PATH = str(
    _DC_AGENT_ROOT / "data" / "config" / "dc_router_config.json"
)
_DC_ROUTER_PLATFORM_IDS = {"巅池-Agent小助手", "巅池-技术（DevOps）", "巅池-技术"}


_SAFE_DEFAULT_CFG = {
    "enabled": False,
    "dry_run": True,
    "fallback_on_error": True,
}


def _read_dc_router_config() -> dict:
    """读 dc-router 开关。任何错误都返回安全默认（enabled=False, dry_run=True）。

    plugin 每次消息调用一次，开关热生效，不需要重启 AstrBot。
    """
    try:
        if not os.path.exists(_DC_ROUTER_CONFIG_PATH):
            return dict(_SAFE_DEFAULT_CFG)
        with open(_DC_ROUTER_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {
            "enabled": bool(data.get("enabled", False)),
            "dry_run": bool(data.get("dry_run", True)),
            "fallback_on_error": bool(data.get("fallback_on_error", True)),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[llm_router] 读 dc_router_config.json 失败，按安全默认处理: %s",
            exc,
        )
        return dict(_SAFE_DEFAULT_CFG)


def _should_enter_dc_router(platform_id: str) -> bool:
    """Platforms that can enter the unified dc-router path."""
    return platform_id in _DC_ROUTER_PLATFORM_IDS


def _normalize_chitchat_text(text: str) -> str:
    cleaned = _CHITCHAT_AT_RE.sub("", text or "")
    cleaned = _CHITCHAT_PUNCT_RE.sub("", cleaned.strip().lower())
    return cleaned


def _chitchat_response_for(text: str) -> str | None:
    normalized = _normalize_chitchat_text(text)
    if not normalized or len(normalized) > _CHITCHAT_MAX_LEN:
        return None
    dynamic_overrides = _read_assistant_language_overrides()
    dynamic_chitchat = dynamic_overrides.get("chitchat", {})
    dynamic_keywords = dynamic_chitchat.get("keywords", {})
    dynamic_responses = dynamic_chitchat.get("responses", {})
    for intent, data in _CHITCHAT_RESPONSES.items():
        keywords = set(data["keywords"])
        keywords.update(dynamic_keywords.get(intent, []))
        if normalized in keywords:
            responses = tuple(dynamic_responses.get(intent, ())) or data["responses"]
            return random.choice(responses)
    return None


def _should_record_chitchat_miss(text: str) -> bool:
    normalized = _normalize_chitchat_text(text)
    if not normalized or len(normalized) > _CHITCHAT_MAX_LEN:
        return False
    return not _CHITCHAT_NEGATIVE_RE.search(normalized)


def _read_assistant_language_overrides() -> dict:
    if load_language_overrides is None:
        return {
            "chitchat": {"keywords": {}, "responses": {}},
            "intent_aliases": [],
            "tone_templates": [],
        }
    try:
        return load_language_overrides(_ASSISTANT_LANGUAGE_OVERRIDES_PATH)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[llm_router] assistant language overrides skipped: %s", exc)
        return {
            "chitchat": {"keywords": {}, "responses": {}},
            "intent_aliases": [],
            "tone_templates": [],
        }


def _inject_assistant_tone_context_into_event(
    event: AstrMessageEvent,
    query_text: str,
) -> bool:
    if not _should_inject_assistant_tone_context(query_text):
        return False
    tone_templates = _assistant_tone_templates_for_business_request(query_text)
    if not tone_templates:
        return False
    current = event.message_str or ""
    if _ASSISTANT_TONE_OPEN_MARKER in current:
        return False
    block = _format_assistant_tone_context(tone_templates)
    event.message_str = f"{current}\n\n{block}" if current else block
    try:
        event.message_obj.message_str = event.message_str
    except Exception:  # noqa: BLE001
        pass
    return True


def _should_inject_assistant_tone_context(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped or len(_normalize_chitchat_text(stripped)) <= _CHITCHAT_MAX_LEN:
        return False
    return bool(_BUSINESS_TONE_RE.search(stripped))


def _assistant_tone_templates_for_business_request(
    query_text: str,
    limit: int = 4,
) -> list[dict]:
    selected: list[dict] = []
    seen_names: set[str] = set()
    for profile in matching_department_memory_profiles(query_text, limit=3):
        name = f"department_profile:{profile.department_id}:{profile.profile_id}"
        selected.append(
            {
                "name": f"{profile.display_name} / {profile.profile_id}",
                "body": profile.tone_template,
                "_dedupe_name": name,
            }
        )
        seen_names.add(name)

    templates = _read_assistant_language_overrides().get("tone_templates", [])
    if not isinstance(templates, list):
        return _strip_internal_template_fields(selected[:limit])
    for template in templates:
        if not isinstance(template, dict):
            continue
        name = str(template.get("name") or "").strip()
        body = str(template.get("body") or "").strip()
        if not name or not body or name in seen_names:
            continue
        selected.append({"name": name, "body": body, "_dedupe_name": name})
        seen_names.add(name)
        if len(selected) >= limit:
            break
    return _strip_internal_template_fields(selected[:limit])


def _strip_internal_template_fields(templates: list[dict]) -> list[dict]:
    return [
        {"name": str(template["name"]), "body": str(template["body"])}
        for template in templates
    ]


def _format_assistant_tone_context(tone_templates: list[dict]) -> str:
    lines = [
        _ASSISTANT_TONE_OPEN_MARKER,
        "以下是已审批的小助手工作风格模板。仅在相关业务任务中参考，不能覆盖用户本轮明确要求：",
    ]
    for template in tone_templates:
        lines.append(f"- {template['name']}: {template['body']}")
    lines.append(_ASSISTANT_TONE_CLOSE_MARKER)
    return "\n".join(lines)


def _has_approved_department_memory_for_prompt(query_text: str, profiles: list) -> bool:
    if not profiles:
        return False
    try:
        from dc_memory_context import retrieve_governed_memory_context

        context = retrieve_governed_memory_context(query_text, limit=8)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[llm_router] department memory prompt lookup skipped: %s", exc)
        return False
    memories = context.get("governed_memories") or []
    department_ids = {getattr(profile, "department_id", "") for profile in profiles}
    profile_names = {getattr(profile, "display_name", "") for profile in profiles}
    for memory in memories:
        if memory.get("review_status") != "approved":
            continue
        if memory.get("sensitivity") not in {"public", "internal"}:
            continue
        haystack = " ".join(
            [
                str(memory.get("owner") or ""),
                str(memory.get("project_id") or ""),
                " ".join(str(tag) for tag in memory.get("tags") or []),
                str(memory.get("title") or ""),
            ]
        )
        if any(
            department_id and department_id in haystack
            for department_id in department_ids
        ):
            return True
        if any(
            profile_name and profile_name in haystack for profile_name in profile_names
        ):
            return True
    return False


def _is_explicit_memory_lookup(text: str) -> bool:
    return bool(_EXPLICIT_MEMORY_LOOKUP_RE.search(text or ""))


def _is_department_memory_confirmation(text: str) -> bool:
    return bool(_DEPARTMENT_MEMORY_CONFIRM_RE.match(text or ""))


def _is_department_memory_dismissal(text: str) -> bool:
    return bool(_DEPARTMENT_MEMORY_DISMISS_RE.match(text or ""))


def _department_memory_suggestion_id(session_key: str, text: str) -> str:
    seed = f"{session_key}:{text}:{int(time.time())}"
    return f"dmpp_{abs(hash(seed)):x}"[:20]


def _department_memory_prompt_text(state: _DepartmentMemoryPromptState) -> str:
    names = "、".join(state.department_names) or "相关部门"
    return (
        f"我检测到这像「{names}」相关任务。\n"
        "是否调用已通过 Obsidian 审核的部门记忆来辅助这次回答？\n"
        f"回复「调用记忆」我会带上；回复「不用」则不调用。本次建议 ID: {state.suggestion_id}"
    )


def _build_department_memory_prompt_card(
    state: _DepartmentMemoryPromptState,
) -> dict[str, Any]:
    names = "、".join(state.department_names) or "相关部门"
    base_value = {
        "source": "department_memory_prompt",
        "suggestion_id": state.suggestion_id,
    }
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "是否调用部门记忆"},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"检测到这像 **{names}** 相关任务。\n"
                    "可以调用已通过 Obsidian 审核的部门记忆辅助回答。"
                ),
            },
            {
                "tag": "markdown",
                "content": (
                    "调用后只作为低优先级参考，不覆盖你本轮明确要求和已提供资料。"
                ),
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "调用记忆"},
                        "type": "primary",
                        "value": {**base_value, "action": "confirm"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "不用"},
                        "type": "default",
                        "value": {**base_value, "action": "dismiss"},
                    },
                ],
            },
        ],
    }


def _append_department_memory_prompt_audit(
    *,
    action: str,
    state: _DepartmentMemoryPromptState,
    status_before: str,
    status_after: str,
    actor: str = "llm_router",
    payload: dict | None = None,
) -> None:
    record = {
        "actor": actor,
        "action": action,
        "suggestion_id": state.suggestion_id,
        "memory_ids": [],
        "department_id": ",".join(state.department_ids),
        "conversation_id": state.conversation_id,
        "status_before": status_before,
        "status_after": status_after,
        "payload": payload or {},
        "timestamp": datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    try:
        _DEPARTMENT_MEMORY_PROMPT_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _DEPARTMENT_MEMORY_PROMPT_AUDIT_PATH.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[llm_router] department memory prompt audit skipped: %s", exc)


def _is_trusted_department_memory_card_action(event: AstrMessageEvent) -> bool:
    msg = getattr(event, "message_obj", None)
    return (
        getattr(event, "is_card_action", False) is True
        or getattr(msg, "is_card_action", False) is True
    )


def _parse_card_action_payload(event: AstrMessageEvent) -> dict[str, Any]:
    payload = getattr(event.message_obj, "card_action_payload", None)
    if isinstance(payload, dict):
        return payload
    text = (event.message_str or "").strip()
    if not text.startswith("__card_action__:"):
        return {}
    try:
        parsed = json.loads(text[len("__card_action__:") :])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _department_memory_card_action_value(event: AstrMessageEvent) -> dict[str, Any]:
    payload = _parse_card_action_payload(event)
    value = payload.get("value", {}) if isinstance(payload, dict) else {}
    if not isinstance(value, dict):
        return {}
    if value.get("source") != "department_memory_prompt":
        return {}
    return value


MediaRouteKind = Literal["image", "text2video", "image2video"]


@dataclass(frozen=True, slots=True)
class MediaRoute:
    kind: MediaRouteKind
    prompt: str
    image_path: str | None = None
    quality: str = "medium"
    aspect_ratio: str = "landscape"


_GPT_IMAGE_MODULE = None
_LAST_IMAGE_BY_SESSION: dict[str, str] = {}

_IMAGE_TRIGGER_RE = re.compile(
    r"(生成|画|绘制|制作|做|设计|创作).{0,8}(图片|图像|插画|海报|封面|头像|壁纸|视觉|素材|照片)"
    r"|(#生图|#画图|#图片|/生图|/画图|/生成图片)",
    re.IGNORECASE,
)
_TEXT_VIDEO_TRIGGER_RE = re.compile(
    r"(文生视频|生成视频|生成动画|制作视频|做视频|做动画|短片|影片|动画短片|#视频|#文生视频|/生成视频)",
    re.IGNORECASE,
)
_IMAGE_VIDEO_TRIGGER_RE = re.compile(
    r"(图生视频|图片转视频|静态图.{0,8}(动画|视频)|动起来|动画化|做成视频|转成视频|加动效|镜头推进)",
    re.IGNORECASE,
)


def _load_gpt_image_module():
    global _GPT_IMAGE_MODULE
    if _GPT_IMAGE_MODULE is not None:
        return _GPT_IMAGE_MODULE
    module_path = Path("/Users/dianchi/DC-Agent/data/plugins/gpt_image_plugin/main.py")
    spec = importlib.util.spec_from_file_location(
        "dc_gpt_image_plugin_main", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("gpt_image_plugin module spec unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _GPT_IMAGE_MODULE = module
    return module


def _extract_generation_prompt(text: str, *, intent: str) -> str:
    stripped = text.strip()
    stripped = re.sub(r"^[/#](生图|画图|图片|生成图片|视频|文生视频)\s*", "", stripped)
    stripped = re.sub(r"^(帮我|请|麻烦|能不能|可以|帮).{0,2}", "", stripped)
    stripped = re.sub(r"^(生成|画|绘制|制作|做|设计|创作|来|给我)", "", stripped)
    stripped = re.sub(r"^(一张|一幅|一个|一段|个|张|幅|段)", "", stripped)
    if intent == "image":
        stripped = re.sub(
            r"(图片|图像|插画|海报|封面|头像|壁纸|视觉|素材|照片)$", "", stripped
        )
    elif intent == "video":
        stripped = re.sub(r"(视频|动画|短片|影片|动效)$", "", stripped)
    elif intent == "image2video":
        stripped = re.sub(
            r"(图生视频|图片转视频|静态图|动画化|动起来|做成视频|转成视频)",
            "",
            stripped,
        )
    stripped = stripped.strip("，。！？,.!? \t")
    return stripped or text.strip()


def _image_quality_from_text(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ("草图", "快速", "低清", "low")):
        return "low"
    if any(
        word in lowered for word in ("高清", "高质量", "精修", "正式", "high", "2k")
    ):
        return "high"
    return "medium"


def _aspect_ratio_from_text(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ("竖版", "海报", "手机", "9:16", "portrait")):
        return "portrait"
    if any(word in lowered for word in ("方图", "方形", "头像", "1:1", "square")):
        return "square"
    return "landscape"


def _dreamina_ratio_from_aspect(aspect_ratio: str) -> str:
    return {"portrait": "9:16", "square": "1:1"}.get(aspect_ratio, "16:9")


def _check_dreamina_status(output: str) -> tuple[bool, str]:
    try:
        json_match = re.search(r'\{.*"gen_status".*\}', output, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            if data.get("gen_status") == "fail":
                return False, str(data.get("fail_reason") or "未知原因")
    except Exception:  # noqa: BLE001
        pass
    return True, ""


def _extract_url(output: str, suffixes: tuple[str, ...]) -> str | None:
    suffix_pattern = "|".join(re.escape(suffix.lstrip(".")) for suffix in suffixes)
    match = re.search(rf'https?://[^\s<>"]+\.(?:{suffix_pattern})[^\s<>"]*', output)
    return match.group() if match else None


@register(
    "llm_router",
    "dc_agent",
    "LLM 智能路由 · 按意图自动选最佳模型（巅池-Agent小助手专用）",
    "1.0.0",
)
class LLMRouterPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._dc_queue_recovery_running = False

    def _ensure_plugin_path(self) -> None:
        import os as _os
        import sys as _sys

        _plugin_dir = _os.path.dirname(_os.path.abspath(__file__))
        if _plugin_dir not in _sys.path:
            _sys.path.insert(0, _plugin_dir)

    async def _send_context_chain(self, umo: str, chain: MessageChain) -> None:
        try:
            await self.context.send_message(umo, chain)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[llm_router] media route 回传失败 umo=%s: %s", umo, exc)

    def _is_group_event(self, event: AstrMessageEvent) -> bool:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if "GroupMessage" in umo or ":group:" in umo.lower():
            return True
        try:
            return bool(event.get_group_id())
        except Exception:  # noqa: BLE001
            return False

    def _sender_rate_key(self, event: AstrMessageEvent) -> str:
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            sender_id = ""
        return f"{event.unified_msg_origin or ''}:{sender_id}"

    def _record_chitchat_miss(self, event: AstrMessageEvent, text: str) -> None:
        if not _should_record_chitchat_miss(text):
            return
        self._append_chitchat_guard_log(_CHITCHAT_MISS_LOG_PATH, event, text)

    def _record_chitchat_hit(self, event: AstrMessageEvent, text: str) -> None:
        self._append_chitchat_guard_log(_CHITCHAT_HIT_LOG_PATH, event, text)

    def _append_chitchat_guard_log(
        self,
        path: Path,
        event: AstrMessageEvent,
        text: str,
    ) -> None:
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            sender_id = ""
        payload = {
            "created_at": datetime.now(UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "platform_id": event.get_platform_id() or "",
            "session_id": event.unified_msg_origin or "",
            "sender_id": sender_id,
            "raw_text": (text or "").strip()[:80],
            "normalized_text": _normalize_chitchat_text(text),
            "source": "llm_router_chitchat_guard",
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[llm_router] chitchat guard log skipped: %s", exc)

    def _try_handle_chitchat_guard(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> bool:
        if self._is_group_event(event) and not getattr(
            event, "is_at_or_wake_command", False
        ):
            return False

        response = _chitchat_response_for(text)
        if response is None:
            self._record_chitchat_miss(event, text)
            return False

        now = time.monotonic()
        rate_key = self._sender_rate_key(event)
        last_hit = _CHITCHAT_LAST_HIT.get(rate_key, 0.0)
        _CHITCHAT_LAST_HIT[rate_key] = now
        if now - last_hit < _CHITCHAT_RATE_LIMIT_SEC:
            response = "我在的，您可以把需要我协助的内容一次发完整，我会尽快处理。"

        self._record_chitchat_hit(event, text)
        event.should_call_llm(False)
        event.set_result(
            MessageEventResult().message(response).use_t2i(False).stop_event()
        )
        logger.info(
            "[llm_router] chitchat guard hit platform=%s text=%r session=%s",
            event.get_platform_id() or "",
            _normalize_chitchat_text(text)[:20],
            event.unified_msg_origin,
        )
        return True

    def _try_handle_department_memory_activation_prompt(
        self,
        event: AstrMessageEvent,
        *,
        raw_text: str,
        query_text: str,
        send_prompt_response: bool = True,
    ) -> _DepartmentMemoryPromptDecision:
        card_action = _department_memory_card_action_value(event)
        if card_action and not _is_trusted_department_memory_card_action(event):
            event.should_call_llm(False)
            event.set_result(
                MessageEventResult().message("").use_t2i(False).stop_event()
            )
            return _DepartmentMemoryPromptDecision(stop=True)

        if self._is_group_event(event) and not getattr(
            event, "is_at_or_wake_command", False
        ):
            return _DepartmentMemoryPromptDecision()

        session_key = self._sender_rate_key(event)
        pending = _PENDING_DEPARTMENT_MEMORY_PROMPTS.get(session_key)
        if pending is not None:
            age = time.monotonic() - pending.created_at
            if age > _DEPARTMENT_MEMORY_PROMPT_TTL_SEC:
                _PENDING_DEPARTMENT_MEMORY_PROMPTS.pop(session_key, None)
                _append_department_memory_prompt_audit(
                    action="expire",
                    state=pending,
                    status_before=pending.status,
                    status_after="expired",
                    payload={"age_sec": round(age, 3)},
                )
                return _DepartmentMemoryPromptDecision()
            card_suggestion_id = str(card_action.get("suggestion_id") or "").strip()
            card_action_name = str(card_action.get("action") or "").strip()
            card_matches_pending = bool(
                card_action
                and card_suggestion_id
                and card_suggestion_id == pending.suggestion_id
            )
            wants_confirm = _is_department_memory_confirmation(raw_text) or (
                card_matches_pending and card_action_name == "confirm"
            )
            wants_dismiss = _is_department_memory_dismissal(raw_text) or (
                card_matches_pending and card_action_name == "dismiss"
            )
            if card_action and not card_matches_pending:
                event.should_call_llm(False)
                event.set_result(
                    MessageEventResult()
                    .message("这条部门记忆建议已经失效，请重新发起当前需求。")
                    .use_t2i(False)
                    .stop_event()
                )
                return _DepartmentMemoryPromptDecision(stop=True)
            if wants_confirm:
                _PENDING_DEPARTMENT_MEMORY_PROMPTS.pop(session_key, None)
                confirmed = _DepartmentMemoryPromptState(
                    suggestion_id=pending.suggestion_id,
                    conversation_id=pending.conversation_id,
                    original_text=pending.original_text,
                    query_text=pending.query_text,
                    department_ids=pending.department_ids,
                    department_names=pending.department_names,
                    profile_ids=pending.profile_ids,
                    created_at=pending.created_at,
                    status="confirmed",
                )
                _append_department_memory_prompt_audit(
                    action="confirm",
                    state=confirmed,
                    status_before=pending.status,
                    status_after="confirmed",
                )
                event.message_str = pending.original_text
                try:
                    event.message_obj.message_str = pending.original_text
                except Exception:  # noqa: BLE001
                    pass
                return _DepartmentMemoryPromptDecision(
                    inject_memory=True,
                    effective_text=pending.original_text,
                    memory_query_text=pending.query_text,
                    suggestion_id=pending.suggestion_id,
                    audit_state=confirmed,
                )
            if wants_dismiss:
                _PENDING_DEPARTMENT_MEMORY_PROMPTS.pop(session_key, None)
                _append_department_memory_prompt_audit(
                    action="dismiss",
                    state=pending,
                    status_before=pending.status,
                    status_after="dismissed",
                )
                event.message_str = pending.original_text
                try:
                    event.message_obj.message_str = pending.original_text
                except Exception:  # noqa: BLE001
                    pass
                return _DepartmentMemoryPromptDecision(
                    dismissed=True,
                    effective_text=pending.original_text,
                    memory_query_text=pending.query_text,
                    suggestion_id=pending.suggestion_id,
                )

        if card_action:
            event.should_call_llm(False)
            event.set_result(
                MessageEventResult()
                .message("这条部门记忆建议已经失效，请重新发起当前需求。")
                .use_t2i(False)
                .stop_event()
            )
            return _DepartmentMemoryPromptDecision(stop=True)

        if _is_explicit_memory_lookup(raw_text):
            return _DepartmentMemoryPromptDecision(
                inject_memory=True,
                effective_text=raw_text,
                memory_query_text=query_text,
            )

        profiles = matching_department_memory_profiles(raw_text, limit=3)
        if not profiles:
            return _DepartmentMemoryPromptDecision(
                inject_memory=True,
                effective_text=raw_text,
                memory_query_text=query_text,
            )
        if not _has_approved_department_memory_for_prompt(query_text, profiles):
            return _DepartmentMemoryPromptDecision(effective_text=raw_text)

        state = _DepartmentMemoryPromptState(
            suggestion_id=_department_memory_suggestion_id(session_key, raw_text),
            conversation_id=session_key,
            original_text=raw_text,
            query_text=query_text,
            department_ids=tuple(profile.department_id for profile in profiles),
            department_names=tuple(profile.display_name for profile in profiles),
            profile_ids=tuple(profile.profile_id for profile in profiles),
            created_at=time.monotonic(),
        )
        _PENDING_DEPARTMENT_MEMORY_PROMPTS[session_key] = state
        _append_department_memory_prompt_audit(
            action="suggest",
            state=state,
            status_before="",
            status_after="suggested",
            payload={"profile_ids": list(state.profile_ids)},
        )
        if send_prompt_response:
            self._set_department_memory_prompt_fallback(event, state)
        logger.info(
            "[llm_router] department memory prompt suggested platform=%s departments=%s",
            event.get_platform_id() or "",
            ",".join(state.department_names),
        )
        return _DepartmentMemoryPromptDecision(
            stop=True,
            effective_text=raw_text,
            memory_query_text=query_text,
            suggestion_id=state.suggestion_id,
            audit_state=state,
        )

    def _set_department_memory_prompt_fallback(
        self,
        event: AstrMessageEvent,
        state: _DepartmentMemoryPromptState,
    ) -> None:
        event.should_call_llm(False)
        event.set_result(
            MessageEventResult()
            .message(_department_memory_prompt_text(state))
            .use_t2i(False)
            .stop_event()
        )

    async def _send_department_memory_prompt_response(
        self,
        event: AstrMessageEvent,
        state: _DepartmentMemoryPromptState,
    ) -> None:
        sent = await self._try_send_department_memory_prompt_card(event, state)
        if sent:
            event.should_call_llm(False)
            event.set_result(
                MessageEventResult().message("").use_t2i(False).stop_event()
            )
            return
        self._set_department_memory_prompt_fallback(event, state)

    async def _try_send_department_memory_prompt_card(
        self,
        event: AstrMessageEvent,
        state: _DepartmentMemoryPromptState,
    ) -> bool:
        if (getattr(event, "get_platform_name", lambda: "")() or "").lower() != "lark":
            return False
        streamer = ensure_streamers_on_context(self.context).get(
            event.get_platform_id() or ""
        )
        if streamer is None:
            return False
        chat_id, receive_id_type = extract_chat_info_from_event(event)
        if not chat_id:
            return False
        card = _build_department_memory_prompt_card(state)
        stream = await send_card_via_runtime(
            streamer,
            card_type="daily_response",
            chat_id=chat_id,
            receive_id_type=receive_id_type,
            card=card,
            platform_id=event.get_platform_id() or "",
            event="start",
            detail=f"department memory prompt {state.suggestion_id}",
        )
        return stream is not None

    async def _run_dreamina_command(
        self,
        command: list[str],
        *,
        timeout: int,
        retries: int = 3,
    ) -> tuple[bool, str]:
        for attempt in range(1, retries + 1):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "dreamina",
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd="/Users/dianchi/DC-Agent",
                )
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout,
                )
            except FileNotFoundError:
                return False, "未找到 dreamina CLI，请先安装并登录"
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.communicate()
                except Exception:  # noqa: BLE001
                    pass
                return False, f"Dreamina 执行超时（{timeout}s）"
            except Exception as exc:  # noqa: BLE001
                return False, f"Dreamina 执行异常: {exc}"

            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            output = stdout + (f"\n错误：{stderr}" if stderr else "")
            if "ExceedConcurrencyLimit" in output and attempt < retries:
                await asyncio.sleep(10 * attempt)
                continue
            return proc.returncode == 0, output or f"Dreamina 返回码 {proc.returncode}"

        return False, "多次重试后仍触发 Dreamina 并发限制，请稍后再试"

    async def _download_url_to_cache(self, url: str, *, suffix: str) -> str:
        cache_dir = Path("/Users/dianchi/DC-Agent/hermes-config/cache/images")
        cache_dir.mkdir(parents=True, exist_ok=True)
        target = cache_dir / f"dreamina_media_{abs(hash(url))}{suffix}"
        await asyncio.to_thread(urllib.request.urlretrieve, url, str(target))
        return str(target)

    async def _first_image_path(self, event: AstrMessageEvent) -> str | None:
        try:
            message = event.message_obj.message
        except Exception:  # noqa: BLE001
            return None
        for comp in message:
            if isinstance(comp, Image):
                try:
                    image_path = await comp.convert_to_file_path()
                    return image_path if image_path else None
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[llm_router] 获取图片附件失败: %s", exc)
                    return None
        return None

    async def _detect_media_route(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> MediaRoute | None:
        text_stripped = text.strip()
        image_path = await self._first_image_path(event)
        session_id = event.unified_msg_origin or ""

        if _IMAGE_VIDEO_TRIGGER_RE.search(text_stripped):
            route_image_path = image_path or _LAST_IMAGE_BY_SESSION.get(session_id)
            if route_image_path:
                return MediaRoute(
                    kind="image2video",
                    prompt=_extract_generation_prompt(
                        text_stripped,
                        intent="image2video",
                    ),
                    image_path=route_image_path,
                )

        if _TEXT_VIDEO_TRIGGER_RE.search(text_stripped):
            return MediaRoute(
                kind="text2video",
                prompt=_extract_generation_prompt(text_stripped, intent="video"),
            )

        if _IMAGE_TRIGGER_RE.search(text_stripped):
            return MediaRoute(
                kind="image",
                prompt=_extract_generation_prompt(text_stripped, intent="image"),
                quality=_image_quality_from_text(text_stripped),
                aspect_ratio=_aspect_ratio_from_text(text_stripped),
            )

        return None

    def _media_task_title(self, route: MediaRoute) -> str:
        return {
            "image": "生图任务",
            "image2video": "图片转视频",
            "text2video": "文生视频",
        }.get(route.kind, "媒体生成")

    def _media_initial_stage(self, route: MediaRoute) -> str:
        return {
            "image": "GPT Image 2 正在生成，失败会自动切 Dreamina",
            "image2video": "Dreamina 正在把静态图动画化",
            "text2video": "Dreamina 正在生成视频",
        }.get(route.kind, "媒体任务处理中")

    async def _start_media_waiting_card(
        self, event: AstrMessageEvent, route: MediaRoute
    ) -> WaitingCardHandle | None:
        return await start_waiting_card_for_event(
            self.context,
            event,
            title=self._media_task_title(route),
            brief=route.prompt or self._media_task_title(route),
            reasoning_tier="high" if route.kind == "image" else "xhigh",
            current_stage=self._media_initial_stage(route),
            interval_sec=5.0,
        )

    async def _finalize_media_waiting_card(
        self,
        card: WaitingCardHandle | None,
        *,
        route: MediaRoute,
        success: bool,
        detail: str,
        output_url: str = "",
        output_path: str = "",
    ) -> bool:
        if card is None:
            return False
        stream = card.streamer.get_stream(card.message_id)
        elapsed_sec = stream.elapsed_sec if stream else 0
        media_kind = (
            "image"
            if route.kind == "image"
            else ("image2video" if route.kind == "image2video" else "video")
        )
        engine = "GPT Image 2 / Dreamina" if route.kind == "image" else "Dreamina 即梦"
        record = build_media_generation_record(
            media_kind=media_kind,
            prompt=route.prompt,
            engine=engine,
            status="succeeded" if success else "failed",
            aspect_ratio=route.aspect_ratio,
            output_url=output_url,
            output_path=output_path,
            error_hint="" if success else detail,
        )
        if success:
            final_card = build_media_generation_card(
                task_title=self._media_task_title(route),
                media_type=media_kind,
                status="已完成",
                prompt=record.to_card_detail(),
                engine=engine,
                task_id=record.record_id,
                aspect_ratio=route.aspect_ratio,
                output_url=output_path or output_url or detail,
                elapsed_sec=elapsed_sec,
            )
        else:
            final_card = build_media_generation_card(
                task_title=self._media_task_title(route),
                media_type=media_kind,
                status="失败",
                prompt=record.to_card_detail(),
                engine=engine,
                task_id=record.record_id,
                aspect_ratio=route.aspect_ratio,
                error_hint=detail,
                elapsed_sec=elapsed_sec,
            )
        return await finalize_card_via_runtime(
            card.streamer,
            card_type="media_generation",
            message_id=card.message_id,
            card=final_card,
            platform_id="",
            detail=f"llm router media finalized: {route.kind} record={record.record_id}",
        )

    async def _run_image_generation_job(
        self,
        umo: str,
        route: MediaRoute,
        card: WaitingCardHandle | None = None,
    ) -> None:
        if card:
            await card.update_stage("GPT Image 2 正在生成")
        module = _load_gpt_image_module()
        structured_prompt = build_structured_media_prompt(
            route.prompt,
            media_kind="image",
            aspect_ratio=route.aspect_ratio,
        )
        loop = asyncio.get_running_loop()
        success, result = await loop.run_in_executor(
            None,
            module._call_codex_image_gen,
            structured_prompt,
            route.quality,
            route.aspect_ratio,
        )

        provider_label = f"GPT Image 2 · {route.quality}"
        if not success:
            gpt_error = result
            if card:
                await card.update_stage("GPT Image 2 暂不可用，已切换 Dreamina 即梦")
            else:
                await self._send_context_chain(
                    umo,
                    MessageChain(
                        [
                            Plain(
                                "GPT Image 2 暂时不可用，已自动切换 Dreamina 即梦继续生图。"
                            )
                        ]
                    ),
                )
            success, result = await loop.run_in_executor(
                None,
                module._dreamina_text2image_sync,
                structured_prompt,
                route.aspect_ratio,
            )
            provider_label = "Dreamina 即梦 · 自动兜底"
            if not success:
                msg = f"生图失败。\nGPT Image 2: {gpt_error}\nDreamina: {result}"
                if not await self._finalize_media_waiting_card(
                    card,
                    route=route,
                    success=False,
                    detail=msg,
                ):
                    await self._send_context_chain(umo, MessageChain([Plain(msg)]))
                return

        _LAST_IMAGE_BY_SESSION[umo] = result
        await self._finalize_media_waiting_card(
            card,
            route=route,
            success=True,
            detail=f"图片已生成，会在下一条消息里发送（{provider_label}）。",
            output_path=result,
        )
        await self._send_context_chain(
            umo,
            MessageChain(
                [
                    Image.fromFileSystem(result),
                    Plain(f"已生成（{provider_label}）。"),
                ]
            ),
        )

    async def _run_dreamina_video_job(
        self,
        umo: str,
        route: MediaRoute,
        card: WaitingCardHandle | None = None,
    ) -> None:
        if route.kind == "image2video":
            if not route.image_path:
                msg = "没有找到可动画化的静态图片。"
                if not await self._finalize_media_waiting_card(
                    card,
                    route=route,
                    success=False,
                    detail=msg,
                ):
                    await self._send_context_chain(umo, MessageChain([Plain(msg)]))
                return
            command = [
                "image2video",
                "--image",
                route.image_path,
                "--prompt",
                build_structured_media_prompt(
                    route.prompt or "animate the scene",
                    media_kind="image2video",
                    aspect_ratio=route.aspect_ratio,
                ),
                "--duration",
                "5",
                "--poll",
                "900",
            ]
            label = "图片转视频"
        else:
            command = [
                "text2video",
                "--prompt",
                build_structured_media_prompt(
                    route.prompt,
                    media_kind="video",
                    aspect_ratio=route.aspect_ratio,
                ),
                "--duration",
                "5",
                "--ratio",
                _dreamina_ratio_from_aspect(route.aspect_ratio),
                "--video_resolution",
                "720p",
                "--poll",
                "900",
            ]
            label = "文生视频"

        if card:
            await card.update_stage(f"{label}处理中，正在等待生成结果")
        success, output = await self._run_dreamina_command(command, timeout=900)
        if not success:
            msg = f"{label}失败：{output}"
            if not await self._finalize_media_waiting_card(
                card,
                route=route,
                success=False,
                detail=msg,
            ):
                await self._send_context_chain(umo, MessageChain([Plain(msg)]))
            return

        gen_ok, fail_reason = _check_dreamina_status(output)
        if not gen_ok:
            msg = f"{label}失败：{fail_reason}"
            if not await self._finalize_media_waiting_card(
                card,
                route=route,
                success=False,
                detail=msg,
            ):
                await self._send_context_chain(umo, MessageChain([Plain(msg)]))
            return

        video_url = _extract_url(output, (".mp4",))
        if not video_url:
            msg = f"{label}完成，但未解析到 mp4 链接：\n{output[:800]}"
            if not await self._finalize_media_waiting_card(
                card,
                route=route,
                success=False,
                detail=msg,
            ):
                await self._send_context_chain(umo, MessageChain([Plain(msg)]))
            return

        try:
            local_video = await self._download_url_to_cache(video_url, suffix=".mp4")
            chain = MessageChain(
                [
                    Video.fromFileSystem(local_video),
                    Plain(f"{label}完成（Dreamina 即梦）。"),
                ]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[llm_router] 下载 Dreamina 视频失败: %s", exc)
            chain = MessageChain([Plain(f"{label}完成：{video_url}")])
        await self._finalize_media_waiting_card(
            card,
            route=route,
            success=True,
            detail=f"{label}已完成，会在下一条消息里发送。",
            output_url=video_url,
            output_path=local_video if "local_video" in locals() else "",
        )
        await self._send_context_chain(umo, chain)

    async def _run_media_route_job(
        self,
        umo: str,
        route: MediaRoute,
        card: WaitingCardHandle | None = None,
    ) -> None:
        try:
            if route.kind == "image":
                await self._run_image_generation_job(umo, route, card)
            else:
                await self._run_dreamina_video_job(umo, route, card)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[llm_router] media route failed kind=%s: %s", route.kind, exc
            )
            msg = f"媒体生成任务失败：{exc}"
            if not await self._finalize_media_waiting_card(
                card,
                route=route,
                success=False,
                detail=msg,
            ):
                await self._send_context_chain(umo, MessageChain([Plain(msg)]))

    async def _try_handle_media_route(self, event: AstrMessageEvent, text: str) -> bool:
        route = await self._detect_media_route(event, text)
        if route is None:
            return False

        event.should_call_llm(False)
        card = await self._start_media_waiting_card(event, route)
        ack = {
            "image": "已进入生图任务：GPT Image 2 主用，Dreamina 即梦自动兜底。",
            "image2video": "已进入图片转视频任务：Dreamina 即梦处理中。",
            "text2video": "已进入文生视频任务：Dreamina 即梦处理中。",
        }[route.kind]
        if card:
            ack = f"{ack}\n等待卡会持续计时，完成后会自动更新。"
        event.set_result(MessageEventResult().message(ack).use_t2i(False).stop_event())
        asyncio.create_task(
            self._run_media_route_job(event.unified_msg_origin, route, card)
        )
        logger.info(
            "[llm_router] media route kind=%s prompt=%r session=%s",
            route.kind,
            route.prompt[:80],
            event.unified_msg_origin,
        )
        return True

    def _start_dc_queue_recovery(self) -> None:
        try:
            self._ensure_plugin_path()
            from dc_router_adapter import start_queue_recovery

            start_queue_recovery(self.context)
            self._dc_queue_recovery_running = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[llm_router] dc-router 队列恢复启动失败: %s", exc)

    def _stop_dc_queue_recovery(self) -> None:
        if not self._dc_queue_recovery_running:
            return
        try:
            self._ensure_plugin_path()
            from dc_router_adapter import stop_queue_recovery

            stop_queue_recovery()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[llm_router] dc-router 队列恢复停止时忽略异常: %s", exc)
        finally:
            self._dc_queue_recovery_running = False

    async def initialize(self) -> None:
        """Start dc-router background helpers only when explicitly enabled."""
        cfg = _read_dc_router_config()
        if not cfg["enabled"] or cfg["dry_run"]:
            return
        self._start_dc_queue_recovery()

    async def terminate(self) -> None:
        self._stop_dc_queue_recovery()

    # ─────────────────── 路由决策 ───────────────────

    def _match_prefix(self, text: str) -> str | None:
        """前缀指令优先（员工显式指定）。"""
        text_strip = text.strip()
        for prefix, intent in PREFIX_INTENTS.items():
            if text_strip.lower().startswith(prefix.lower()):
                return intent
        return None

    def _match_reasoning_prefix(self, text: str) -> str | None:
        """识别推理级别前缀 → 直接返 provider_id（最高优先级）。"""
        text_strip = text.strip()
        for prefix, provider_id in REASONING_PREFIX_PROVIDERS.items():
            if text_strip.lower().startswith(prefix.lower()):
                return provider_id
        return None

    def _match_keywords(self, text: str) -> str | None:
        """关键词兜底（LLM 失败时用）。"""
        for item in _read_assistant_language_overrides().get("intent_aliases", []):
            if not isinstance(item, dict):
                continue
            intent = str(item.get("intent") or "")
            pattern = str(item.get("pattern") or "")
            if intent not in INTENT_TO_PROVIDER or not pattern:
                continue
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    return intent
            except re.error as exc:
                logger.debug(
                    "[llm_router] invalid assistant intent alias pattern=%r: %s",
                    pattern,
                    exc,
                )
        for intent, pattern in _KEYWORD_RULES:
            if pattern.search(text):
                return intent
        return None

    async def _classify_with_llm(self, text: str) -> str | None:
        """用小 LLM 判断意图。超时 / 失败 → 返 None。"""
        try:
            providers = {p.meta().id: p for p in self.context.get_all_providers()}
            router_provider = providers.get(ROUTER_LLM_PROVIDER)
            if not router_provider:
                logger.warning(
                    "[llm_router] router LLM %s 不存在，跳过 LLM 判定",
                    ROUTER_LLM_PROVIDER,
                )
                return None

            # 调小 LLM
            resp = await asyncio.wait_for(
                router_provider.text_chat(
                    prompt=text[:500],
                    system_prompt=ROUTER_SYSTEM_PROMPT,
                    contexts=[],
                ),
                timeout=8,
            )

            raw = (resp.completion_text or "").strip()
            m = _JSON_RE.search(raw)
            if not m:
                logger.debug("[llm_router] LLM 输出无 JSON: %r", raw[:100])
                return None

            data = json.loads(m.group(0))
            intent = str(data.get("intent", "")).strip().lower()
            if intent in INTENT_TO_PROVIDER:
                return intent
        except asyncio.TimeoutError:
            logger.warning("[llm_router] LLM 判定超时 (>8s)，回退到关键词")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[llm_router] LLM 判定失败: %s", exc)
        return None

    # ─────────────────── on_message 钩子 ───────────────────

    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE
    )
    async def route(self, event: AstrMessageEvent) -> None:
        platform_id = event.get_platform_id() or ""
        # ─── dc-router 开关检查 ───
        # 默认 enabled=false，行为跟原 v1.0 完全一样。
        # enabled=true 时调 dc-router 新路径（depth=DIRECT 已接，FRONT/HERMES 步骤 4 之后接）；
        # 新路径出任何错 → fallback_on_error=true 兜底回 v1.0（员工不感知）。
        _dc_cfg = _read_dc_router_config()
        if (
            not _dc_cfg["enabled"] or _dc_cfg["dry_run"]
        ) and self._dc_queue_recovery_running:
            self._stop_dc_queue_recovery()

        text = event.message_str or ""
        raw_user_text = text
        card_action_resumed = False
        department_memory_prompt_dismissed = False
        if text.startswith("__card_action__:"):
            department_memory_card_decision = (
                self._try_handle_department_memory_activation_prompt(
                    event,
                    raw_text=raw_user_text,
                    query_text=raw_user_text,
                )
            )
            if department_memory_card_decision.stop:
                return
            if department_memory_card_decision.inject_memory:
                raw_user_text = department_memory_card_decision.effective_text
                text = raw_user_text
                card_action_resumed = True
            elif department_memory_card_decision.dismissed:
                raw_user_text = department_memory_card_decision.effective_text
                text = raw_user_text
                card_action_resumed = True
                department_memory_prompt_dismissed = True
            else:
                if _department_memory_card_action_value(event):
                    return

            if not card_action_resumed:
                try:
                    self._ensure_plugin_path()
                    from dc_router_adapter import (
                        maybe_handle_antigravity_queue_card_action,
                    )

                    handled = await maybe_handle_antigravity_queue_card_action(
                        self.context, event
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[llm_router] 排队卡按钮处理失败: %s", exc)
                    # 异常时直接退出，避免把异常状态带入下游 LLM 路由
                    return
                if handled:
                    # 是 antigravity 排队卡，已被 adapter 处理（含 stop_event/set_result）
                    return
                # 不是 antigravity 卡（pet / 其他自定义卡）→ dc-router 不接管，
                # 但也绝不能让卡片回调进入下游 LLM 路由（否则被分类为 casual 走 LLM）。
                # 直接 return 让事件落到其他 plugin 的 @filter.regex("^__card_action__:") handler。
                logger.debug(
                    "[llm_router] 非 antigravity 卡片回调，让其他 plugin 接管: %s",
                    text[:80],
                )
                return

        if _should_enter_dc_router(platform_id) and self._try_handle_chitchat_guard(
            event,
            raw_user_text,
        ):
            return

        if _should_enter_dc_router(platform_id) and _dc_cfg.get("enabled", False):
            try:
                self._ensure_plugin_path()
                from dc_memory_context import inject_memory_context_into_event
                from truth_intake_guard import maybe_handle_truth_intake

                if await maybe_handle_truth_intake(self.context, event, _dc_cfg):
                    return
                memory_query_text = await build_memory_retrieval_query(
                    self.context,
                    event,
                )
                if department_memory_prompt_dismissed:
                    memory_prompt_decision = _DepartmentMemoryPromptDecision(
                        effective_text=raw_user_text,
                        memory_query_text=memory_query_text,
                    )
                else:
                    memory_prompt_decision = (
                        self._try_handle_department_memory_activation_prompt(
                            event,
                            raw_text=raw_user_text,
                            query_text=memory_query_text,
                            send_prompt_response=False,
                        )
                    )
                if memory_prompt_decision.stop:
                    if memory_prompt_decision.audit_state is not None:
                        await self._send_department_memory_prompt_response(
                            event,
                            memory_prompt_decision.audit_state,
                        )
                    return
                if memory_prompt_decision.effective_text:
                    raw_user_text = memory_prompt_decision.effective_text
                    text = raw_user_text
                if (
                    memory_prompt_decision.inject_memory
                    and inject_memory_context_into_event(
                        event,
                        query_text=memory_prompt_decision.memory_query_text
                        or memory_query_text,
                    )
                ):
                    if memory_prompt_decision.audit_state is not None:
                        _append_department_memory_prompt_audit(
                            action="apply",
                            state=memory_prompt_decision.audit_state,
                            status_before="confirmed",
                            status_after="confirmed",
                            payload={"applied": True},
                        )
                    get_extra = getattr(event, "get_extra", None)
                    hits = (
                        get_extra("dc_agent_memory_hits") if callable(get_extra) else {}
                    )
                    hits = hits or {}
                    logger.info(
                        "[llm_router] dc-memory injected platform=%s docs=%s items=%s",
                        platform_id,
                        hits.get("documents", 0),
                        hits.get("project_items", 0),
                    )
                if _inject_assistant_tone_context_into_event(event, raw_user_text):
                    logger.info(
                        "[llm_router] assistant tone context injected platform=%s",
                        platform_id,
                    )
                text = raw_user_text
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[llm_router] truth intake guard 失败，继续路由: %s", exc
                )

        if _should_enter_dc_router(platform_id) and await self._try_handle_media_route(
            event,
            raw_user_text,
        ):
            return

        if _dc_cfg["enabled"] and _should_enter_dc_router(platform_id):
            try:
                # lazy import：只在 enabled=true 时把 plugin 目录加入 sys.path
                # 并 import adapter（避免 enabled=false 时引入新依赖）
                self._ensure_plugin_path()
                from dc_router_adapter import route_via_dc_router

                if not _dc_cfg["dry_run"]:
                    self._start_dc_queue_recovery()

                memory_injected_text = event.message_str
                memory_injected_message_obj_text = getattr(
                    event.message_obj,
                    "message_str",
                    None,
                )
                event.message_str = raw_user_text
                try:
                    event.message_obj.message_str = raw_user_text
                except Exception:  # noqa: BLE001
                    pass
                try:
                    handled = await route_via_dc_router(
                        self.context, event, dry_run=_dc_cfg["dry_run"]
                    )
                finally:
                    event.message_str = memory_injected_text
                    try:
                        event.message_obj.message_str = memory_injected_message_obj_text
                    except Exception:  # noqa: BLE001
                        pass
                if handled:
                    return  # dc-router 已接管，停止后续 v1.0 逻辑
                # dry_run 模式下 handled 永远为 False（让 v1.0 实际处理 + dc-router 已写日志）
                logger.debug(
                    "[llm_router] dc-router 未接管本消息（dry_run=%s），fallback 到 v1.0",
                    _dc_cfg["dry_run"],
                )
            except Exception as exc:  # noqa: BLE001
                if _dc_cfg["fallback_on_error"]:
                    logger.warning(
                        "[llm_router] dc-router 调用异常，fallback 到 v1.0: %s",
                        exc,
                    )
                else:
                    logger.error(
                        "[llm_router] dc-router 异常且 fallback_on_error=false，"
                        "停止本消息处理: %s",
                        exc,
                    )
                    return

        elif _dc_cfg["enabled"]:
            logger.debug(
                "[llm_router] dc-router skip platform=%s (not in allowlist)",
                platform_id,
            )

        mode = ENABLED_PLATFORMS.get(platform_id)
        if not mode:
            return

        text_stripped = text.strip()
        if not text_stripped or len(text_stripped) < 2:
            return

        target_provider_id: str | None = None
        source: str = ""
        intent: str | None = None

        # ─── 最高优先级：推理级别前缀（#高 / #超深 / #中 等，所有 platform 都生效）───
        reasoning_pinned = self._match_reasoning_prefix(text_stripped)
        if reasoning_pinned:
            target_provider_id = reasoning_pinned
            source = "reasoning_prefix"

        # ─── 模式 A：固定 pin provider（如 巅池-技术 → codex/gpt-5.4）───
        elif mode != "intent":
            target_provider_id = mode
            source = "platform_pin"

        # ─── 模式 B：3 层意图路由 ───
        else:
            # 1) 前缀指令
            intent = self._match_prefix(text_stripped)
            if intent:
                source = "prefix"
            else:
                # 2) LLM 意图识别
                intent = await self._classify_with_llm(text_stripped)
                if intent:
                    source = "llm"
                else:
                    # 3) 关键词兜底
                    intent = self._match_keywords(text_stripped)
                    if intent:
                        source = "keyword"

            if not intent:
                # 完全没命中 → 不动，走全局默认
                return

            target_provider_id = INTENT_TO_PROVIDER.get(intent)
            if not target_provider_id:
                return

        # 验证目标 provider 存在
        available = {p.meta().id for p in self.context.get_all_providers()}
        if target_provider_id not in available:
            logger.warning(
                "[llm_router] target provider %s 不在 available 列表，跳过",
                target_provider_id,
            )
            return

        # 切 provider（按 umo 会话级隔离）
        try:
            await self.context.provider_manager.set_provider(
                provider_id=target_provider_id,
                provider_type=ProviderType.CHAT_COMPLETION,
                umo=event.unified_msg_origin,
            )
            # 把推理级别存到 event，daily_card_renderer 等下游能读
            tier = None
            if "xhigh" in target_provider_id:
                tier = "xhigh"
            elif "high" in target_provider_id:
                tier = "high"
            elif "medium" in target_provider_id:
                tier = "medium"
            if tier:
                event.set_extra("reasoning_tier", tier)
            event.set_extra("llm_router_intent", intent or "")
            event.set_extra("llm_router_source", source)
            event.set_extra("llm_router_provider", target_provider_id)
            logger.info(
                "[llm_router] %s · platform=%s · intent=%s · provider=%s · tier=%s · '%s'",
                source,
                platform_id,
                intent or "-",
                target_provider_id,
                tier or "-",
                text_stripped[:30].replace("\n", " "),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[llm_router] set_provider 失败 (%s): %s",
                target_provider_id,
                exc,
            )
