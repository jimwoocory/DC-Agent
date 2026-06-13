"""Assistant tone context injection; set_extra only, never mutates message_str."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

from astrbot.api import logger

ASSISTANT_TONE_OPEN_MARKER: Final[str] = "<assistant_tone_context>"
ASSISTANT_TONE_CLOSE_MARKER: Final[str] = "</assistant_tone_context>"

_BUSINESS_TONE_RE: Final[re.Pattern] = re.compile(
    r"(视频|脚本|文案|分镜|拍摄|选题|传播|混剪|纪录片|采访|发布|转发|封面|"
    r"五菱|缤果|星光|MINIEV|mini|菱骏|红标|扬光|宝骏|柳汽|东风|风行|乘龙|菱智)",
    re.IGNORECASE,
)
_CHITCHAT_MAX_LEN: Final[int] = 8
_PUNCT_RE: Final[re.Pattern] = re.compile(
    r"[\s，。！？、~～?!\.,;；:：\"'“”‘’（）()【】\[\]{}<>《»]+"
)
_AT_RE: Final[re.Pattern] = re.compile(r"^\s*(?:\[At:[^\]]+\]|@[^\s]+\s*)+")


def _normalize_chitchat_text(text: str) -> str:
    cleaned = _AT_RE.sub("", text or "")
    return _PUNCT_RE.sub("", cleaned.strip().lower())


def _should_inject(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped or len(_normalize_chitchat_text(stripped)) <= _CHITCHAT_MAX_LEN:
        return False
    return bool(_BUSINESS_TONE_RE.search(stripped))


def _load_overrides() -> dict:
    try:
        from dc_engines.assistant_distillation import load_language_overrides

        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
    except Exception:  # noqa: BLE001
        return {"tone_templates": []}
    try:
        path = (
            Path(get_astrbot_data_path())
            / "config"
            / "assistant_language_overrides.json"
        )
        return load_language_overrides(path) or {"tone_templates": []}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc_router] assistant language overrides skipped: %s", exc)
        return {"tone_templates": []}


def _gather_tone_templates(query_text: str, *, limit: int = 4) -> list[dict]:
    selected: list[dict] = []
    seen: set[str] = set()
    try:
        from dc_engines.department_workflows.memory_profiles import (
            matching_department_memory_profiles,
        )
    except Exception:  # noqa: BLE001
        matching_department_memory_profiles = None
    if matching_department_memory_profiles is not None:
        try:
            for profile in matching_department_memory_profiles(query_text, limit=3):
                key = f"department_profile:{profile.department_id}:{profile.profile_id}"
                selected.append(
                    {
                        "name": f"{profile.display_name} / {profile.profile_id}",
                        "body": profile.tone_template,
                    }
                )
                seen.add(key)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[dc_router] dept profile tone match skipped: %s", exc)

    templates = _load_overrides().get("tone_templates") or []
    if not isinstance(templates, list):
        return selected[:limit]
    for tpl in templates:
        if not isinstance(tpl, dict):
            continue
        name = str(tpl.get("name") or "").strip()
        body = str(tpl.get("body") or "").strip()
        if not name or not body or name in seen:
            continue
        selected.append({"name": name, "body": body})
        seen.add(name)
        if len(selected) >= limit:
            break
    return selected[:limit]


def try_inject_assistant_tone(event: Any, query_text: str) -> bool:
    """True if tone context was attached to event.set_extra (no message_str pollution)."""
    if not _should_inject(query_text):
        return False
    templates = _gather_tone_templates(query_text)
    if not templates:
        return False
    try:
        event.set_extra(
            "dc_router_tone_context",
            {
                "templates": templates,
                "open_marker": ASSISTANT_TONE_OPEN_MARKER,
                "close_marker": ASSISTANT_TONE_CLOSE_MARKER,
            },
        )
    except Exception:  # noqa: BLE001
        return False
    return True


__all__ = [
    "ASSISTANT_TONE_CLOSE_MARKER",
    "ASSISTANT_TONE_OPEN_MARKER",
    "try_inject_assistant_tone",
]
