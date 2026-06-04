"""满意度检测器

检测员工对 AstrBot 上一次回复的不满意信号，触发 Hermes 升级链路。

检测层级：
  1. 显式 Hermes 请求（置信度 0.98）：员工直接要求交给 Hermes
  2. 高置信度不满（置信度 0.88）：明确表达回答不够深入/需要深度研究
  3. 中置信度不满（置信度 0.70）：泛化不满语句（需配合活跃任务上下文才升级）
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class SatisfactionSignal:
    """满意度检测结果。"""

    dissatisfied: bool
    confidence: float
    reason: str
    matched_keyword: str = ""
    is_explicit_hermes_request: bool = False


class SatisfactionDetector:
    """关键词 + 规则满意度检测器，无需 LLM 调用，轻量快速。"""

    # ── 层级 1：员工显式要求 Hermes 处理 ──────────────────────────────────────
    _HERMES_EXPLICIT_PATTERNS: list[str] = [
        r"hermes\s*(来|处理|搞|执行|查|帮我)",
        r"(交给|让|叫|请)\s*hermes",
        r"hermes",  # 兜底：只要含 hermes 就视为显式请求
    ]

    # ── 层级 2：高置信度不满 ─────────────────────────────────────────────────
    # 明确指向"回答不够深/需深入研究"
    _HIGH_CONFIDENCE_KEYWORDS: list[str] = [
        # 对当前回答的评价
        "不够详细",
        "太简单了",
        "太简单",
        "不够深入",
        "太浅了",
        "太浅",
        "太肤浅",
        "不够深",
        "太表面",
        "过于笼统",
        "太笼统",
        # 对研究深度的需求
        "深入研究",
        "深度研究",
        "深度分析",
        "全面研究",
        "系统研究",
        "仔细研究",
        "详细研究",
        "全面分析",
        "彻底研究",
        # 对查找深度的需求
        "帮我查清楚",
        "查得更仔细",
        "查得更详细",
        "查深一点",
        "深挖一下",
        "好好查一查",
        "认真查一下",
        "彻底查一下",
        "查得全面一点",
        "查得彻底一点",
    ]

    # ── 层级 3：中置信度不满（需活跃任务上下文才升级）────────────────────────
    _MEDIUM_CONFIDENCE_KEYWORDS: list[str] = [
        "再查查",
        "多查一些",
        "多找些",
        "再详细点",
        "还不够",
        "你再看看",
        "重新查",
        "重新分析",
        "再分析一下",
        "能不能查",
        "帮我查一下",
        "再深入点",
        "更深入",
        "查仔细",
        "多说一点",
        "能不能更详细",
        "再完整一点",
        "补充一下",
        "再补充",
        "不够完整",
    ]

    def detect(self, message: str) -> SatisfactionSignal:
        """
        检测消息中的不满信号。

        Returns:
            SatisfactionSignal，dissatisfied=True 时调用方考虑升级。
        """
        text = message.strip()
        text_lower = text.lower()

        # 层级 0：显式前缀指令短路
        # #深度 / #PRD / #洞察 / #创意 / #代码 / #舆情 等是员工"主动指令"，
        # 本质上是路由请求（应该被 dc-router 或 v1 reasoning_prefix 处理），
        # 而不是"对前一条回复不满意"。不该被 detector 误判为升级信号。
        # 2026-05-19 修复：避免 hermes_escalation 抢断 dc-router 的 #深度 路径。
        if text.startswith("#"):
            return SatisfactionSignal(
                dissatisfied=False,
                confidence=0.0,
                reason="explicit_prefix_command_skip",
            )

        # 层级 1：显式 Hermes 请求
        for pattern in self._HERMES_EXPLICIT_PATTERNS:
            if re.search(pattern, text_lower, re.IGNORECASE):
                return SatisfactionSignal(
                    dissatisfied=True,
                    confidence=0.98,
                    reason="explicit_hermes_request",
                    matched_keyword=pattern,
                    is_explicit_hermes_request=True,
                )

        # 层级 2：高置信度不满关键词
        for kw in self._HIGH_CONFIDENCE_KEYWORDS:
            if kw in text_lower:
                return SatisfactionSignal(
                    dissatisfied=True,
                    confidence=0.88,
                    reason="high_confidence_dissatisfaction",
                    matched_keyword=kw,
                )

        # 层级 3：中置信度不满关键词
        for kw in self._MEDIUM_CONFIDENCE_KEYWORDS:
            if kw in text_lower:
                return SatisfactionSignal(
                    dissatisfied=True,
                    confidence=0.70,
                    reason="medium_confidence_dissatisfaction",
                    matched_keyword=kw,
                )

        return SatisfactionSignal(
            dissatisfied=False,
            confidence=0.0,
            reason="no_dissatisfaction_signal",
        )
