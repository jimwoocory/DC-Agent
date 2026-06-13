"""Reasoning prefix parser for dc_router.

#高 / #超深 / #codex 这类前缀由员工显式指定 provider_id（最高优先级，
不论 platform、不论 dc_router 是否 enabled）。命中后 set_provider + 跳过
所有意图分类。

旧 ``INTENT_TO_PROVIDER`` / ``PREFIX_INTENTS`` 路由表（v1.0 死代码）已删除。
"""

from __future__ import annotations

from typing import Final

from astrbot.api import logger

# Gemini via AIHubMix
# 重要语义边界: ``#超深`` / ``#超高`` / ``#xhigh`` / ``#深度`` 一律走 Claude Opus 4-7
# (员工日常「给我往深了想」的最高档)。**只有显式带 codex 头的** (例如 ``#codex超深``)
# 才走 codex 系列 — codex 是「跨模型对照 / 备用」通道,不是默认升级路径。
# 改这个表时必须同时更新 tests/dc_router/test_reasoning_prefix.py 和
# tests/dc_router/test_dispatch_pipeline.py 的 TestStage4ReasoningPrefix。
# 历史教训: 2026-06-11 用户反馈 ``#超深`` 走 codex 是不对的, 这条注释是
# 防御性边界 — 任何后续 PR 把 ``#超深`` 改回 codex 系列都会触发 match_reasoning_prefix
# 里的 assert / 单元测试, 防止线上再次出现「深度任务用错了模型」。
_CLAUDE_FAMILY_PREFIXES: Final[frozenset[str]] = frozenset(
    {"#超深", "#超高", "#xhigh", "#深度"}
)

_REASONING_PREFIX_PROVIDERS: Final[dict[str, str]] = {
    "#中": "aihubmix/gemini-3.5-flash",
    "#medium": "aihubmix/gemini-3.5-flash",
    "#fast": "aihubmix/gemini-3.5-flash",
    "#高": "aihubmix/claude-sonnet-4-6",
    "#high": "aihubmix/claude-sonnet-4-6",
    "#超深": "aihubmix/claude-opus-4-7",
    "#超高": "aihubmix/claude-opus-4-7",
    "#xhigh": "aihubmix/claude-opus-4-7",
    "#深度": "aihubmix/claude-opus-4-7",
    # Codex 三档（备用 / 跨模型对照）— 必须显式写 #codex... 才生效
    "#codex": "codex/gpt-5.5-medium",
    "#codex高": "codex/gpt-5.5-high",
    "#codex超深": "codex/gpt-5.5-xhigh",
}

_PREFIX_KEYS_LOWER: Final[tuple[tuple[str, str], ...]] = tuple(
    (prefix.lower(), provider_id)
    for prefix, provider_id in _REASONING_PREFIX_PROVIDERS.items()
)


def match_reasoning_prefix(text: str) -> str | None:
    """Return the pinned provider_id if the user text starts with a reasoning prefix.

    Case-insensitive. Strips leading whitespace before matching.

    Longest-prefix match: ``#codex高`` wins over ``#codex`` because both
    would match the input literally — we have to pick the *most specific*
    prefix the employee typed. The table contains both, and iterating in
    insertion order would let the shorter ``#codex`` shadow the longer
    ``#codex高``, which is a real production bug — the user explicitly
    typed the upgrade marker and should not silently get the medium tier.

    Defensive invariant (2026-06-11): ``#超深`` / ``#超高`` / ``#xhigh`` /
    ``#深度`` 命中后, 命中的 provider_id 必须属于 Claude 系列. 一旦表里
    有人手滑写回 codex 系列, 这里会 ``logger.error`` + 在 CI 测试里被
    ``test_xhigh_prefix_uses_claude_series_not_codex`` 拦截。
    """
    if not text:
        return None
    head = text.lstrip().lower()
    best_prefix_len = 0
    best_provider_id: str | None = None
    best_prefix_matched: str | None = None
    for prefix, provider_id in _PREFIX_KEYS_LOWER:
        if head.startswith(prefix) and len(prefix) > best_prefix_len:
            best_prefix_len = len(prefix)
            best_provider_id = provider_id
            best_prefix_matched = prefix
    if best_prefix_matched is not None and best_provider_id is not None:
        # 还原最具体前缀的原始大小写 (用于查找 _CLAUDE_FAMILY_PREFIXES)
        original_prefix = next(
            (
                raw
                for raw in _REASONING_PREFIX_PROVIDERS
                if raw.lower() == best_prefix_matched
            ),
            best_prefix_matched,
        )
        if original_prefix in _CLAUDE_FAMILY_PREFIXES and "codex" in best_provider_id:
            # 这一支正常情况下不可达 — 但它防止未来有人误改表而线上静默路由到 codex.
            logger.error(
                "[dc_router] REASONING PREFIX INVARIANT VIOLATED: prefix=%r → "
                "provider=%r (must be Claude series, not codex). "
                "检查 _REASONING_PREFIX_PROVIDERS 表, 同时跑 "
                "tests/dc_router/test_reasoning_prefix.py::"
                "test_xhigh_prefix_uses_claude_series_not_codex 验证修复。",
                original_prefix,
                best_provider_id,
            )
    return best_provider_id


_STRIP_PREFIXES: Final[tuple[str, ...]] = (
    "#深度",
    "#PRD",
    "#prd",
    "#洞察",
    "#创意",
    "#舆情",
    "#代码",
    "#高",
    "#中",
    "#超深",
    "#超高",
    "#medium",
    "#fast",
    "#high",
    "#xhigh",
    "#codex",
    "#codex高",
    "#codex超深",
)


def strip_known_prefix(text: str) -> str:
    """Strip a known dc-router prefix from the text, falling back to the input.

    Used by the CLI prompt builder so the downstream LLM does not echo
    "#深度 做一份品牌分析" verbatim — we want it to focus on the actual task.

    Longest-prefix match (mirrors :func:`match_reasoning_prefix`) — if
    the user typed ``#codex超深`` we want to strip the whole escalation
    marker, not just ``#codex`` and leave a trailing ``超深`` in the prompt.
    """
    stripped = text.lstrip()
    lowered = stripped.lower()
    best_len = 0
    for prefix in _STRIP_PREFIXES:
        pl = prefix.lower()
        if lowered.startswith(pl) and len(pl) > best_len:
            best_len = len(pl)
    if best_len == 0:
        return text
    remainder = stripped[best_len:].lstrip()
    return remainder or text


# 重新导出 — 兼容老代码 ``from routing import REASONING_PREFIX_PROVIDERS``
REASONING_PREFIX_PROVIDERS = _REASONING_PREFIX_PROVIDERS


__all__ = [
    "REASONING_PREFIX_PROVIDERS",
    "match_reasoning_prefix",
    "strip_known_prefix",
]
