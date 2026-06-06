"""Deterministic routing rules."""

from __future__ import annotations

import re
from dataclasses import dataclass

from dc_router.taxonomy import RouterIntent


@dataclass(frozen=True, slots=True)
class RuleMatch:
    intent: RouterIntent
    reason: str
    source: str


PREFIX_RULES: tuple[tuple[str, RouterIntent, str], ...] = (
    ("#深度", RouterIntent.DEEP_INSIGHT, "User forced a deep task."),
    ("#PRD", RouterIntent.DEEP_INSIGHT, "User forced a PRD or deep planning task."),
    ("#prd", RouterIntent.DEEP_INSIGHT, "User forced a PRD or deep planning task."),
    ("#前置", RouterIntent.WORK_PREFLIGHT, "User forced work preflight routing."),
    ("#轻文案", RouterIntent.WORK_PREFLIGHT, "User forced lightweight copy routing."),
    ("#洞察", RouterIntent.INSIGHT, "User forced insight routing."),
    ("#创意", RouterIntent.CREATIVE, "User forced creative routing."),
    ("#舆情", RouterIntent.PUBLIC_OPINION, "User forced public opinion routing."),
    ("#代码", RouterIntent.SIMPLE_CODE, "User forced code routing."),
)

FEISHU_DOCUMENT_URL_RE = re.compile(
    r"https?://[^\s]*?feishu\.cn/(wiki|docx|docs)/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
PRD_TOKEN_RE = r"(?<![A-Za-z0-9_])prd(?![A-Za-z0-9_])"
PRD_TASK_RE = re.compile(
    rf"(帮我|请|麻烦|给我|需要|想要|我要|希望你|帮忙).{{0,20}}{PRD_TOKEN_RE}"
    rf"|(生成|起草|整理|输出|完善|设计|分析|评估|拆解).{{0,16}}{PRD_TOKEN_RE}"
    rf"|{PRD_TOKEN_RE}.{{0,16}}(文档|需求|方案|大纲|框架|草稿|模板|产品|功能|页面|模块)",
    re.IGNORECASE,
)
DEEP_TASK_RE = re.compile(
    r"(深度|完整|系统|全面|彻底|详细|详尽).{0,18}(分析|方案|报告|策略|拆解|规划|研究|评估)"
    r"|(分析|方案|报告|策略|拆解|规划|研究|评估).{0,18}(深度|完整|系统|全面|彻底|详细|详尽)",
    re.IGNORECASE,
)

# 顺序敏感: 强信号意图（PUBLIC_OPINION, DEEP_*, SIMPLE_CODE）放在前面，
# 避免泛匹配（如 "脚本" 出现在 "Python 脚本" 和 "短视频脚本" 两种语义里）。
KEYWORD_RULES: tuple[tuple[RouterIntent, tuple[str, ...], str], ...] = (
    (
        RouterIntent.PUBLIC_OPINION,
        (
            "舆情",
            "危机公关",
            "危机回应",
            "热点攻防",
            "舆论",
            "公关话术",
            "负面评论",
            "舆情管理",
        ),
        "Matched public opinion keywords.",
    ),
    (
        RouterIntent.DEEP_CREATIVE,
        (
            "深度营销方案",
            "完整营销方案",
            "营销报告",
            "campaign strategy",
            "整合营销",
        ),
        "Matched deep creative keywords.",
    ),
    (
        RouterIntent.DEEP_INSIGHT,
        (
            "深度洞察",
            "深度分析",
            "品牌战略报告",
            "用户研究报告",
            "完整战略",
        ),
        "Matched deep insight keywords.",
    ),
    (
        RouterIntent.SIMPLE_CODE,
        (
            "报错",
            "bug",
            "小脚本",
            "代码",
            "traceback",
            "stack trace",
            "TypeError",
            "ValueError",
            "Python",
            "JavaScript",
            "脚本报错",
        ),
        "Matched simple code keywords.",
    ),
    (
        RouterIntent.WORK_PREFLIGHT,
        (
            "帮我看看这个怎么处理",
            "这个怎么处理",
            "帮我想一下",
            "帮我想想",
            "我有个事情想问",
            "我有个问题想问",
            "先帮我理一下",
            "先帮我梳理",
            "帮我润色一句",
            "按钮文案",
            "页面文案",
            "前端文案",
            "短话术",
            "轻文案",
        ),
        "Matched work preflight or lightweight copy keywords.",
    ),
    (
        RouterIntent.CREATIVE,
        (
            "slogan",
            "口号",
            "广告语",
            "短视频脚本",
            "营销文案",
            "campaign",
            "爆款文案",
            "宣传标题",
        ),
        "Matched creative keywords.",
    ),
    (
        RouterIntent.INSIGHT,
        (
            "品牌战略",
            "用户洞察",
            "用户画像",
            "品牌定位",
            "人群定位",
            "竞品分析",
            "消费者洞察",
            "市场洞察",
        ),
        "Matched insight keywords.",
    ),
    (
        RouterIntent.OPS_WRITING,
        (
            "通知",
            "公告",
            "日报",
            "周报",
            "邮件",
            "会议纪要",
            "办公文书",
        ),
        "Matched operations writing keywords.",
    ),
    (
        RouterIntent.CASUAL,
        (
            "你好",
            "在吗",
            "在不在",
            "早上好",
            "晚上好",
            "下午好",
            "hello",
            "hi",
            "我的工作方法",
            "我习惯",
            "我一般",
            "我通常",
        ),
        "Matched casual chat keywords.",
    ),
    (
        RouterIntent.REALTIME,
        (
            "最新",
            "热点",
            "趋势",
            "实时",
            "新闻",
            "搜索",
        ),
        "Matched realtime keywords.",
    ),
)


def match_prefix(text: str) -> RuleMatch | None:
    stripped = text.lstrip()
    for prefix, intent, reason in PREFIX_RULES:
        if stripped.startswith(prefix):
            return RuleMatch(intent=intent, reason=reason, source="prefix")
    return None


def match_document_link(text: str) -> RuleMatch | None:
    if FEISHU_DOCUMENT_URL_RE.search(text):
        return RuleMatch(
            intent=RouterIntent.MULTIMODAL,
            reason="Matched Feishu document URL for inline document understanding.",
            source="document_link",
        )
    return None


def match_keywords(text: str) -> RuleMatch | None:
    if PRD_TASK_RE.search(text):
        return RuleMatch(
            intent=RouterIntent.DEEP_INSIGHT,
            reason="Matched explicit PRD task request.",
            source="keyword",
        )
    normalized = text.lower()
    for intent, keywords, reason in KEYWORD_RULES:
        if intent not in {RouterIntent.DEEP_CREATIVE, RouterIntent.DEEP_INSIGHT}:
            continue
        if any(keyword.lower() in normalized for keyword in keywords):
            return RuleMatch(intent=intent, reason=reason, source="keyword")

    if DEEP_TASK_RE.search(text):
        return RuleMatch(
            intent=RouterIntent.DEEP_INSIGHT,
            reason="Matched explicit deep analysis task request.",
            source="keyword",
        )

    for intent, keywords, reason in KEYWORD_RULES:
        if intent in {RouterIntent.DEEP_CREATIVE, RouterIntent.DEEP_INSIGHT}:
            continue
        if any(keyword.lower() in normalized for keyword in keywords):
            return RuleMatch(intent=intent, reason=reason, source="keyword")
    return None
