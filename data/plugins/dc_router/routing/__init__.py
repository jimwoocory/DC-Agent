"""Routing subsystem — pure routing logic for dc_router plugin.

模块列表:
- ``event_envelope``: AstrMessageEvent → MessageEnvelope (pure)
- ``reasoning_prefix``: 解析 #高/#超深 等直接 pin provider_id 的前缀
- ``apply_decision``: 把 RouterDecision 落到 AstrBot (set_provider + extras)
- ``legacy_v1_fallback``: dc_router 关闭/dry-run 时保留的 v1.0 兼容路径
"""

from .apply_decision import (
    annotate_event_with_decision,
    apply_decision,
    apply_provider_pin,
)
from .event_envelope import build_envelope
from .legacy_v1_fallback import (
    classify_intent_v1,
    reason_with_llm_v1,
)
from .reasoning_prefix import (
    REASONING_PREFIX_PROVIDERS,
    match_reasoning_prefix,
    strip_known_prefix,
)

__all__ = [
    "REASONING_PREFIX_PROVIDERS",
    "annotate_event_with_decision",
    "apply_decision",
    "apply_provider_pin",
    "build_envelope",
    "classify_intent_v1",
    "match_reasoning_prefix",
    "reason_with_llm_v1",
    "strip_known_prefix",
]
