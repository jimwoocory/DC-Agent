"""Provider mapping for the new DC router path."""

from __future__ import annotations

from dataclasses import dataclass

from dc_router_core.taxonomy import RouteAction, RouteDepth, RouterIntent

AIHUBMIX_GEMINI_FLASH = "aihubmix/gemini-3.5-flash"
AIHUBMIX_GEMINI_PRO = "aihubmix/gemini-3.1-pro-preview"
AIHUBMIX_QWEN_FLASH = "aihubmix/qwen3.6-flash"
AIHUBMIX_QWEN_MAX = "aihubmix/qwen3.7-max"
AIHUBMIX_DEEPSEEK_PRO = "aihubmix/deepseek-v4-pro"
AIHUBMIX_DOUBAO_SEED_2_1_PRO = "aihubmix/doubao-seed-2-1-pro"
AIHUBMIX_GROK = "aihubmix/grok-4.3"
AIHUBMIX_CLAUDE_SONNET_4_6 = "aihubmix/claude-sonnet-4-6"
AIHUBMIX_CLAUDE_OPUS_4_7 = "aihubmix/claude-opus-4-7"
AIHUBMIX_CLAUDE_OPUS_4_8 = "aihubmix/claude-opus-4-8"
CLI_GROK_BUILD = "cli/grok-build"
CLI_CODEX_GPT_5_4 = "cli/codex/gpt-5.4"
CODEX_GPT_5_5_FALLBACK = "codex/gpt-5.5-xhigh"
GEMINI_3_1_PRO = AIHUBMIX_GEMINI_PRO
CODEX_GPT_5_5_HIGH = "codex/gpt-5.5-high"


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    intent: RouterIntent
    provider_id: str
    depth: RouteDepth
    action: RouteAction
    target_model: str | None = None
    resource_keys: tuple[str, ...] = ()
    requires_queue: bool = False
    requires_harness: bool = False
    description: str = ""


DEFAULT_PROVIDER_MAP: dict[RouterIntent, ProviderRoute] = {
    # 2026-06-25: local Gemini CLI is retired from default business routing.
    # Ordinary chat and lightweight work now use Qwen Max directly.
    RouterIntent.CASUAL: ProviderRoute(
        intent=RouterIntent.CASUAL,
        provider_id=AIHUBMIX_QWEN_MAX,
        target_model="qwen3.7-max",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="Casual chat through AIHubMix Qwen 3.7 Max.",
    ),
    RouterIntent.WORK_PREFLIGHT: ProviderRoute(
        intent=RouterIntent.WORK_PREFLIGHT,
        provider_id=AIHUBMIX_QWEN_MAX,
        target_model="qwen3.7-max",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="Work preflight and lightweight copy through AIHubMix Qwen 3.7 Max.",
    ),
    RouterIntent.OPS_WRITING: ProviderRoute(
        intent=RouterIntent.OPS_WRITING,
        provider_id=AIHUBMIX_QWEN_MAX,
        target_model="qwen3.7-max",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="Office writing and routine operational drafts through AIHubMix Qwen 3.7 Max.",
    ),
    RouterIntent.MULTIMODAL: ProviderRoute(
        intent=RouterIntent.MULTIMODAL,
        provider_id=AIHUBMIX_GEMINI_FLASH,
        target_model="gemini-3.5-flash",
        depth=RouteDepth.DIRECT,
        action=RouteAction.PREPROCESS,
        description="Image, screenshot, voice, video, and file understanding through AIHubMix Gemini 3.5 Flash.",
    ),
    RouterIntent.REALTIME: ProviderRoute(
        intent=RouterIntent.REALTIME,
        provider_id=AIHUBMIX_QWEN_MAX,
        target_model="qwen3.7-max",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="Realtime and search-like questions through AIHubMix Qwen 3.7 Max; Brave remains the factual search source.",
    ),
    RouterIntent.PUBLIC_OPINION: ProviderRoute(
        intent=RouterIntent.PUBLIC_OPINION,
        provider_id=CLI_GROK_BUILD,
        target_model="grok-build",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="Public opinion, crisis response, and hot-topic defense through Grok Build, with AIHubMix fallback.",
    ),
    RouterIntent.SIMPLE_CODE: ProviderRoute(
        intent=RouterIntent.SIMPLE_CODE,
        provider_id=CLI_CODEX_GPT_5_4,
        target_model="gpt-5.4",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="Small scripts, error explanations, and simple fixes through Codex CLI.",
    ),
    RouterIntent.CREATIVE: ProviderRoute(
        intent=RouterIntent.CREATIVE,
        provider_id=AIHUBMIX_DOUBAO_SEED_2_1_PRO,
        target_model="doubao-seed-2-1-pro",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="High-value planning, marketing copy, slogans, and scripts through AIHubMix Doubao Seed 2.1 Pro.",
    ),
    RouterIntent.INSIGHT: ProviderRoute(
        intent=RouterIntent.INSIGHT,
        provider_id=AIHUBMIX_CLAUDE_SONNET_4_6,
        target_model="claude-sonnet-4-6",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="Brand strategy and user insight through AIHubMix Claude Sonnet 4.6.",
    ),
    RouterIntent.DEEP_CREATIVE: ProviderRoute(
        intent=RouterIntent.DEEP_CREATIVE,
        provider_id=AIHUBMIX_CLAUDE_OPUS_4_7,
        target_model="claude-opus-4-7",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="Deep creative planning through AIHubMix Claude Opus 4.7.",
    ),
    RouterIntent.DEEP_INSIGHT: ProviderRoute(
        intent=RouterIntent.DEEP_INSIGHT,
        provider_id=AIHUBMIX_CLAUDE_OPUS_4_8,
        target_model="claude-opus-4-8",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="Deep insight and strategic analysis through AIHubMix Claude Opus 4.8.",
    ),
    RouterIntent.FALLBACK: ProviderRoute(
        intent=RouterIntent.FALLBACK,
        provider_id=AIHUBMIX_QWEN_MAX,
        target_model="qwen3.7-max",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="Fallback for unclear non-garbage messages through AIHubMix Qwen 3.7 Max.",
    ),
}


def get_provider_route(intent: RouterIntent) -> ProviderRoute:
    return DEFAULT_PROVIDER_MAP.get(intent, DEFAULT_PROVIDER_MAP[RouterIntent.FALLBACK])
