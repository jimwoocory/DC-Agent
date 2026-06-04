"""Shared taxonomy for the DC router."""

from __future__ import annotations

from enum import Enum


class RouterIntent(str, Enum):
    CASUAL = "casual"
    OPS_WRITING = "ops_writing"
    MULTIMODAL = "multimodal"
    REALTIME = "realtime"
    PUBLIC_OPINION = "public_opinion"
    SIMPLE_CODE = "simple_code"
    CREATIVE = "creative"
    INSIGHT = "insight"
    DEEP_CREATIVE = "deep_creative"
    DEEP_INSIGHT = "deep_insight"
    FALLBACK = "fallback"


class RouteDepth(str, Enum):
    DIRECT = "direct"
    FRONT = "front"
    HERMES = "hermes"


class RouteAction(str, Enum):
    PREPROCESS = "preprocess"
    ANSWER = "answer"
    QUEUE_FRONT = "queue_front"
    ENQUEUE_DEEP_TASK = "enqueue_deep_task"


class AttachmentKind(str, Enum):
    IMAGE = "image"
    SCREENSHOT = "screenshot"
    VOICE = "voice"
    VIDEO = "video"
    FILE = "file"
