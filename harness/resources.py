"""Scarce LLM resource definitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ResourceKey(str, Enum):
    GEMINI_OAUTH_3_1_PRO = "gemini_oauth_3_1_pro"
    GEMINI_CLI_PRO = "gemini_cli_pro"
    CLAUDE_OAUTH_GLOBAL = "claude_oauth_global"
    CLAUDE_OAUTH_SONNET_4_6 = "claude_oauth_sonnet_4_6"
    CLAUDE_OAUTH_OPUS_4_7 = "claude_oauth_opus_4_7"
    CLAUDE_CLI_GLOBAL = "claude_cli_global"
    CLAUDE_CLI_OPUS_4_7 = "claude_cli_opus_4_7"
    CLAUDE_CLI_SONNET_4_6 = "claude_cli_sonnet_4_6"
    CODEX_CLI_GLOBAL = "codex_cli_global"
    CODEX_CLI_HIGH = "codex_cli_high"
    CODEX_CLI_XHIGH = "codex_cli_xhigh"
    ANTIGRAVITY_CLI_FLASH = "antigravity_cli_flash"


@dataclass(frozen=True, slots=True)
class ResourceConfig:
    key: str
    max_concurrency: int = 1
    cooldown_after_completion_seconds: int = 10 * 60
    estimated_run_seconds: int = 10 * 60


# ---------------------------------------------------------------------------
# 架构：双路径并存（CLI 默认 + OAuth 备用），长期保留
# ---------------------------------------------------------------------------
# 当前默认路径：各家 CLI（claude / gemini / hermes-agent），走订阅，
# 服务端 5h 滚动窗口管 quota，本地仅需 30 秒短冷却防同秒并发。
#
# 备用路径：OAuth + API key，长期保留在位（vendor 政策可能变、订阅
# 失效、或 CI / 批量任务 / 企业接入仍需要 API-key 鉴权的场景）。
# 不要把 CLAUDE_OAUTH_* / GEMINI_OAUTH_* 当 legacy 清掉。
#
# 类默认 cooldown=10min 是按 OAuth 配额节奏给的保底值；CLI 资源
# 在下方逐条 override 成 30 秒。两条路径共用同一套 quota_gate /
# queue_store，路径选择由 router 决策、HermesBridge 分发。
# ---------------------------------------------------------------------------
DEFAULT_RESOURCE_CONFIGS: dict[str, ResourceConfig] = {
    ResourceKey.GEMINI_OAUTH_3_1_PRO.value: ResourceConfig(
        key=ResourceKey.GEMINI_OAUTH_3_1_PRO.value,
    ),
    ResourceKey.GEMINI_CLI_PRO.value: ResourceConfig(
        key=ResourceKey.GEMINI_CLI_PRO.value,
    ),
    ResourceKey.CLAUDE_OAUTH_GLOBAL.value: ResourceConfig(
        key=ResourceKey.CLAUDE_OAUTH_GLOBAL.value,
    ),
    ResourceKey.CLAUDE_OAUTH_SONNET_4_6.value: ResourceConfig(
        key=ResourceKey.CLAUDE_OAUTH_SONNET_4_6.value,
    ),
    ResourceKey.CLAUDE_OAUTH_OPUS_4_7.value: ResourceConfig(
        key=ResourceKey.CLAUDE_OAUTH_OPUS_4_7.value,
    ),
    # Claude CLI 不撞 OAuth 同源限制（CLI 是 Claude Code 本体），
    # 不需要 30 分钟冷却。Claude Max 订阅本身有 5 小时滚动窗口，
    # 由 Anthropic 服务端控制，本地 cooldown 帮不上忙反而把员工卡死。
    # 2026-05-20 修：CLI 资源冷却改成 30 秒（短间隔防止并发撞同一秒触发限速）。
    ResourceKey.CLAUDE_CLI_GLOBAL.value: ResourceConfig(
        key=ResourceKey.CLAUDE_CLI_GLOBAL.value,
        cooldown_after_completion_seconds=30,
    ),
    ResourceKey.CLAUDE_CLI_OPUS_4_7.value: ResourceConfig(
        key=ResourceKey.CLAUDE_CLI_OPUS_4_7.value,
        cooldown_after_completion_seconds=30,
    ),
    ResourceKey.CLAUDE_CLI_SONNET_4_6.value: ResourceConfig(
        key=ResourceKey.CLAUDE_CLI_SONNET_4_6.value,
        cooldown_after_completion_seconds=30,
    ),
    # Codex CLI resources (dpr protocol via codex binary).
    # Short cooldown like other CLIs; Codex has its own quota windows.
    ResourceKey.CODEX_CLI_GLOBAL.value: ResourceConfig(
        key=ResourceKey.CODEX_CLI_GLOBAL.value,
        cooldown_after_completion_seconds=30,
    ),
    ResourceKey.CODEX_CLI_HIGH.value: ResourceConfig(
        key=ResourceKey.CODEX_CLI_HIGH.value,
        cooldown_after_completion_seconds=30,
    ),
    ResourceKey.CODEX_CLI_XHIGH.value: ResourceConfig(
        key=ResourceKey.CODEX_CLI_XHIGH.value,
        cooldown_after_completion_seconds=30,
    ),
    # Gemini CLI 同理 — 走 Code Assist OAuth，限速由 Google 控（每天 1500-2000 次），
    # 不是单次 30 分钟冷却。
}

# 也把 Gemini CLI 改成短冷却（同上）
DEFAULT_RESOURCE_CONFIGS[ResourceKey.GEMINI_CLI_PRO.value] = ResourceConfig(
    key=ResourceKey.GEMINI_CLI_PRO.value,
    cooldown_after_completion_seconds=30,
)

# Antigravity CLI/Gemini Flash 临时保护阈值：
# - 官方可核验口径按 15 RPM 保守理解，系统暂按 12 RPM 以内落地。
# - 单并发 + 5 秒冷却，20 人同时闲聊时由 router 短排队/快速切备用。
DEFAULT_RESOURCE_CONFIGS[ResourceKey.ANTIGRAVITY_CLI_FLASH.value] = ResourceConfig(
    key=ResourceKey.ANTIGRAVITY_CLI_FLASH.value,
    cooldown_after_completion_seconds=5,
    estimated_run_seconds=10,
)
