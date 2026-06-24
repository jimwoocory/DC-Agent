"""Skill preloader — 在 LLM 调用前预读匹配 skill 的 SKILL.md 并注入 system_prompt.

Why this exists (2026-06-11):
    普通 aihubmix LLM 在收到 "请基于 brand-marketing skill 帮我做营销方案" 这类
    任务时, 经常 hallucinate 说 "我正使用 astrbot_file_read_tool 读取 brand-
    marketing..." 但**不真的调 tool** (截图里看到的假工具调用). 让 LLM 真的能
    拿到 skill 内容的办法: 在 routing 阶段 (LLM 调用前) 主动预读匹配 skill 的
    SKILL.md 全文, 注入到 system_prompt. 这样 LLM 拿到真东西, hallucinate
    tool use 自动消除.

设计:
    1. **on_llm_request 钩子**:  在 LLM 真的发起请求前最后一刻触发, 拿到
       ``ProviderRequest`` 直接改 ``system_prompt`` 字段.
    2. **意图来源**:  读 ``event.get_extra("dc_router_intent")`` (由 dc_router
       routing 阶段写入). 如果没拿到 intent 就降级到纯文本匹配 (不限 intent).
    3. **附件检测**:  ``has_attachments=True`` 时, 强制把 ``document-intake``
       skill 加进候选, 让 LLM 知道怎么处理附件.
    4. **幂等性**:  ``req.system_prompt`` 已包含 skill 注入标记时, 不重复注入.
       防止 multi-turn LLM 调用时插件被多次触发.
    5. **异常隔离**:  skill 目录不存在 / 解析失败 / IO 错误, 全部静默 skip,
       不影响主链路 LLM 调用.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# 让 skill_loader 可被 import (项目根的 dc_router_core).
DC_ROOT = Path(__file__).resolve().parents[3]
if str(DC_ROOT) not in sys.path:
    sys.path.insert(0, str(DC_ROOT))

from astrbot.api import logger  # noqa: E402
from astrbot.api.event import AstrMessageEvent, filter  # noqa: E402
from astrbot.api.star import Context, Star, register  # noqa: E402
from astrbot.core.provider.entities import ProviderRequest  # noqa: E402
from dc_router_core.skill_loader import (  # noqa: E402
    build_skill_prompt_block,
    match_skill_for_intent,
)
from dc_router_core.taxonomy import RouterIntent  # noqa: E402

# 注入标记 — 防止 multi-turn 重复注入 (在 system_prompt 末尾加 marker,
# 下次注入时若 marker 已存在则 skip).
_INJECTION_MARKER = "[AstrBot skill hints — 以下技能库内容已预读注入"

# 文本里包含 trigger marker (前缀路由) 时不注入 — 用户显式指定了模型/路径,
# 让 LLM 走纯 prompt, 避免被预读内容带跑偏.
_PREFIX_SKIP_TEXT = "#"

# 注入的 system_prompt 段总字节上限 (4 KB). 与 skill_loader 的 default 一致.
_MAX_INJECTION_BYTES = 4096


def _detect_attachments(event: Any) -> bool:
    """Best-effort 检测 event 是否有图片/文件/音视频附件.

    用 duck-typing 避免插件启动期硬依赖 astrbot 组件类型.
    """
    msg_obj = getattr(event, "message_obj", None)
    if msg_obj is None:
        return False
    components = getattr(msg_obj, "message", None)
    if not isinstance(components, list):
        return False
    # 任何非纯文本 component 视为有附件
    for comp in components:
        if comp is None:
            continue
        type_name = type(comp).__name__
        if type_name in {"Image", "Record", "Video", "File", "At", "AtAll", "Reply"}:
            return True
        # 兜底: 任何带 file/url/path 属性的 component 也算
        if any(
            getattr(comp, attr, None)
            for attr in ("file", "url", "path", "file_id", "image_key")
        ):
            return True
    return False


def _read_intent(event: Any) -> str:
    """从 event 读取 dc_router 写入的 intent. 找不到返回空串."""
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return ""
    try:
        return str(getter("dc_router_intent") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _read_text(event: Any) -> str:
    """从 event 拿用户消息文本."""
    try:
        return str(event.message_str or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _already_injected(system_prompt: str) -> bool:
    """检查 system_prompt 是否已被本插件注入过 (避免 multi-turn 重复)."""
    if not system_prompt:
        return False
    return _INJECTION_MARKER in system_prompt


@register(
    "skill_preloader",
    "dc_agent",
    "Skill 预读注入 — 解决 LLM 假装调 astrbot_file_read_tool 的 hallucination",
    "1.0.0",
)
class SkillPreloaderPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._log_config_once()

    def _log_config_once(self) -> None:
        """首启时打印配置快照, 方便排查 skills 路径."""
        try:
            from dc_router_core.skill_loader import (
                get_skills_root,
                list_available_skills,
            )

            root = get_skills_root()
            skills = list_available_skills()
            logger.info(
                "[skill_preloader] 启动: skills_root=%s, 已发现 %d 个 skill: %s",
                root,
                len(skills),
                ", ".join(s.name for s in skills) or "(none)",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[skill_preloader] 启动期扫描失败: %s", exc)

    @filter.on_llm_request(priority=20)
    async def inject_matched_skills(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """LLM 调用前注入匹配的 SKILL.md 内容到 system_prompt."""
        original = req.system_prompt or ""

        # 1) 前缀路由的显式指令 (#xxx) 跳过 — 用户已经指定路径, 不要被预读带偏.
        text = _read_text(event)
        if text.startswith(_PREFIX_SKIP_TEXT):
            return

        # 2) 已被注入过 (multi-turn 二次调用) 跳过.
        if _already_injected(original):
            return

        # 3) 读 intent.
        intent_str = _read_intent(event)
        intent: RouterIntent | str = intent_str
        if intent_str:
            try:
                intent = RouterIntent(intent_str)
            except ValueError:
                intent = intent_str  # 解析失败时退回 str (skill_loader 内部容错)

        # 4) 检测附件.
        has_attachments = _detect_attachments(event)

        # 5) 匹配 skills.
        try:
            matched = match_skill_for_intent(
                intent,
                text,
                has_attachments=has_attachments,
                max_matches=2,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[skill_preloader] match_skill_for_intent 失败: %s", exc)
            return

        if not matched:
            return  # 没匹配上 — 不污染 system_prompt

        # 6) 拼装注入段.
        try:
            block = build_skill_prompt_block(
                matched, max_total_bytes=_MAX_INJECTION_BYTES
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[skill_preloader] build_skill_prompt_block 失败: %s", exc)
            return

        if not block:
            return

        req.system_prompt = original + block

        logger.info(
            "[skill_preloader] 注入 %d 个 skill → %s (intent=%s, attachments=%s)",
            len(matched),
            ", ".join(s.name for s in matched),
            intent_str or "(none)",
            has_attachments,
        )


__all__ = ["SkillPreloaderPlugin"]
