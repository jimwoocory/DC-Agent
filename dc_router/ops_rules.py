"""Ops router 前缀 + 关键词规则。

跟 business rules 完全独立，关键词聚焦在运维/技术/后台监控场景。
"""

from __future__ import annotations

from dataclasses import dataclass

from dc_router.ops_taxonomy import OpsIntent


@dataclass(frozen=True, slots=True)
class OpsRuleMatch:
    intent: OpsIntent
    reason: str
    source: str


# 顺序敏感: 强信号意图 (QUEUE_STATUS, QUOTA_GATE_VIEW, ERROR_DEBUG)
# 放在前面避免被 CODE_OPS / DEPLOYMENT_OPS 泛匹配吃掉
OPS_KEYWORD_RULES: tuple[tuple[OpsIntent, tuple[str, ...], str], ...] = (
    (
        OpsIntent.QUOTA_GATE_VIEW,
        (
            "凭证池",
            "credential pool",
            "OAuth 状态",
            "aihubmix 用量",
            "配额闸门",
            "QuotaGate",
            "quota gate",
            "API 配额",
            "限速状态",
        ),
        "Matched quota gate view keywords.",
    ),
    (
        OpsIntent.QUEUE_STATUS,
        (
            "队列",
            "queue status",
            "排队",
            "深度任务",
            "冷却",
            "cooldown",
            "失败任务",
            "DLQ",
            "dead letter",
        ),
        "Matched queue status keywords.",
    ),
    (
        OpsIntent.SYSTEM_STATUS,
        (
            "Hermes 状态",
            "AstrBot 状态",
            "服务状态",
            "链路状态",
            "system status",
            "看门狗",
            "watchdog",
            "进程状态",
            "服务健康",
        ),
        "Matched system status keywords.",
    ),
    (
        OpsIntent.ERROR_DEBUG,
        (
            "报错",
            "stack trace",
            "traceback",
            "异常排查",
            "日志解读",
            "看日志",
            "log 分析",
            "error 解释",
            "排障",
            "故障定位",
        ),
        "Matched error debug keywords.",
    ),
    (
        OpsIntent.DEPLOYMENT_OPS,
        (
            "部署",
            "deploy",
            "重启服务",
            "服务重启",
            "git pull",
            "git push",
            "git rebase",
            "build",
            "构建",
            "CI/CD",
            "上线",
        ),
        "Matched deployment ops keywords.",
    ),
    (
        OpsIntent.CODE_OPS,
        (
            "小脚本",
            "shell 脚本",
            "bash 脚本",
            "写个脚本",
            "Python 代码",
            "JavaScript 代码",
            "代码片段",
            "code snippet",
        ),
        "Matched code ops keywords.",
    ),
)


# 显式前缀 (员工手动指定)
OPS_PREFIX_RULES: tuple[tuple[str, OpsIntent, str], ...] = (
    ("#状态", OpsIntent.SYSTEM_STATUS, "Forced system status."),
    ("#队列", OpsIntent.QUEUE_STATUS, "Forced queue status."),
    ("#配额", OpsIntent.QUOTA_GATE_VIEW, "Forced quota gate view."),
    ("#排障", OpsIntent.ERROR_DEBUG, "Forced error debug."),
    ("#部署", OpsIntent.DEPLOYMENT_OPS, "Forced deployment ops."),
    ("#脚本", OpsIntent.CODE_OPS, "Forced code ops."),
)


def match_ops_prefix(text: str) -> OpsRuleMatch | None:
    stripped = text.lstrip()
    for prefix, intent, reason in OPS_PREFIX_RULES:
        if stripped.startswith(prefix):
            return OpsRuleMatch(intent=intent, reason=reason, source="prefix")
    return None


def match_ops_keywords(text: str) -> OpsRuleMatch | None:
    normalized = text.lower()
    for intent, keywords, reason in OPS_KEYWORD_RULES:
        if any(keyword.lower() in normalized for keyword in keywords):
            return OpsRuleMatch(intent=intent, reason=reason, source="keyword")
    return None
