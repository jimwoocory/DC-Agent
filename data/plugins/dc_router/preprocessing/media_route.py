"""Media route — image / video / image2video generation.

检测 trigger regex → 选 provider → 走 GPT Image 2 / Dreamina CLI →
发等待卡 → 完成后发图片 / 视频。本模块不阻塞主消息（创建 asyncio task 跑后台）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import re
import time
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from dc_engines.dreamina_cli import (
    dreamina_command_not_found_message,
    resolve_dreamina_executable,
)

from astrbot.api import logger
from astrbot.api.event import MessageChain, MessageEventResult
from astrbot.api.message_components import Image as ImageComp
from astrbot.api.message_components import Plain
from astrbot.api.message_components import Video as VideoComp

from ..paths import data_path, project_root

GPT_IMAGE_MODULE_PATH: Final[Path] = data_path("plugins", "gpt_image_plugin", "main.py")
HERMES_CACHE_DIR: Final[Path] = project_root() / "hermes-config" / "cache" / "images"
_MEDIA_TASKS_PATH: Final[Path] = data_path("runtime", "media_route_pending.json")
_MEDIA_RECOVERY_TASK: asyncio.Task | None = None
_ACTIVE_MEDIA_TASKS: dict[str, Any] = {}
_ACTIVE_MEDIA_CONTEXT: dict[str, tuple[str, MediaRoute, Any]] = {}

MediaRouteKind = Literal["image", "text2video", "image2video"]
ImageProviderStrategy = Literal["gpt_first", "dreamina_first"]


@dataclass(frozen=True, slots=True)
class MediaRoute:
    kind: MediaRouteKind
    prompt: str
    image_path: str | None = None
    quality: str = "medium"
    aspect_ratio: str = "landscape"
    image_provider_strategy: ImageProviderStrategy = "gpt_first"


_IMAGE_TRIGGER_RE: Final[re.Pattern] = re.compile(
    r"(生成|画|绘制|制作|做|设计|创作).{0,8}(图片|图像|插画|海报|封面|头像|壁纸|视觉|素材|照片)"
    r"|(需要|想要|要|帮我|给我).{0,20}(一张|一幅|一个|张|幅|个)?.{0,12}(图片|图像|插画|海报|封面|头像|壁纸|视觉|素材|照片)"
    r"|(#生图|#画图|#图片|/生图|/画图|/生成图片)",
    re.IGNORECASE,
)
_TEXT_VIDEO_TRIGGER_RE: Final[re.Pattern] = re.compile(
    r"(文生视频|生成视频|生成动画|制作视频|做视频|做动画|短片|影片|动画短片|#视频|#文生视频|/生成视频)",
    re.IGNORECASE,
)
_IMAGE_VIDEO_TRIGGER_RE: Final[re.Pattern] = re.compile(
    r"(图生视频|图片转视频|静态图.{0,8}(动画|视频)|动起来|动画化|做成视频|转成视频|加动效|镜头推进)",
    re.IGNORECASE,
)
_DREAMINA_FIRST_RE: Final[re.Pattern] = re.compile(
    r"(用|走|调用)?\s*(dreamina|即梦|剪映即梦)",
    re.IGNORECASE,
)
_EXPLICIT_IMAGE_REQUEST_RE: Final[re.Pattern] = re.compile(
    r"(生成|画|绘制|制作|做|设计|创作|出|产出).{0,8}"
    r"(一张|一幅|张|幅|套|组).{0,20}"
    r"(图片|图像|插画|海报|封面|头像|壁纸|主视觉|视觉图|效果图|素材|照片)"
    r"|(帮我|给我|请|麻烦).{0,8}"
    r"(生成|画|绘制|制作|做|设计|创作|出|产出).{0,20}"
    r"(图片|图像|插画|海报|封面|头像|壁纸|主视觉|视觉图|效果图|素材|照片)"
    r"|(#生图|#画图|#图片|/生图|/画图|/生成图片)",
    re.IGNORECASE,
)
_PLAN_CONTEXT_RE: Final[re.Pattern] = re.compile(
    r"(方案|规划|执行方案|落地方案|运营方案|策划案|计划|框架|拆解|细化|策略打法)",
    re.IGNORECASE,
)
_SOURCE_IMAGE_REF_RE: Final[re.Pattern] = re.compile(
    r"(\[image\]|这张|这幅|这个|图片|图像|照片|相片|原图|附件图|上传的图)",
    re.IGNORECASE,
)
_SOURCE_IMAGE_EDIT_RE: Final[re.Pattern] = re.compile(
    r"(去掉背景|去背景|去除背景|移除背景|删除背景|背景透明|透明底|"
    r"抠图|抠出来|抠出|人物抠|人像抠|提取人物|保留人物|主体分离)",
    re.IGNORECASE,
)
_SOURCE_IMAGE_EDIT_REFINEMENT_RE: Final[re.Pattern] = re.compile(
    r"(头发|发丝|边缘|毛边|轮廓|细节|主体|人物).{0,12}"
    r"(不理想|不好|不干净|粗糙|再精细|更精细|精细一点|优化|修一下|再处理|重新处理)"
    r"|(?:再|更)?精细(?:一点|一些)?|不理想|毛边|发丝",
    re.IGNORECASE,
)
_SOURCE_IMAGE_EDIT_REPROCESS_RE: Final[re.Pattern] = re.compile(
    r"(执行精修|确认精修|开始精修|直接精修|重新处理|再处理|重新抠|再抠|"
    r"再跑一次|重新跑一次|直接处理|用高级抠图|用精修)",
    re.IGNORECASE,
)
_SOURCE_IMAGE_EDIT_STATE_TTL_SEC: Final[float] = 30 * 60

_LAST_IMAGE_BY_SESSION: dict[str, str] = {}
_LAST_SOURCE_IMAGE_EDIT_BY_SESSION: dict[str, dict[str, Any]] = {}
_GPT_IMAGE_MODULE: Any = None


def _load_gpt_image_module() -> Any:
    global _GPT_IMAGE_MODULE
    if _GPT_IMAGE_MODULE is not None:
        return _GPT_IMAGE_MODULE
    spec = importlib.util.spec_from_file_location(
        "dc_gpt_image_plugin_main", GPT_IMAGE_MODULE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("gpt_image_plugin module spec unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _GPT_IMAGE_MODULE = module
    return module


def _extract_generation_prompt(text: str, *, intent: str) -> str:
    stripped = text.strip()
    stripped = re.sub(r"^[/#](生图|画图|图片|生成图片|视频|文生视频)\s*", "", stripped)
    stripped = re.sub(r"^(帮我|请|麻烦|能不能|可以|帮).{0,2}", "", stripped)
    stripped = re.sub(r"^(生成|画|绘制|制作|做|设计|创作|来|给我)", "", stripped)
    stripped = re.sub(r"^(一张|一幅|一个|一段|个|张|幅|段)", "", stripped)
    if intent == "image":
        stripped = re.sub(r"(图片|图像|素材|照片)$", "", stripped)
    elif intent == "video":
        stripped = re.sub(r"(视频|动画|短片|影片|动效)$", "", stripped)
    elif intent == "image2video":
        stripped = re.sub(
            r"(图生视频|图片转视频|静态图|动画化|动起来|做成视频|转成视频)",
            "",
            stripped,
        )
    return stripped.strip("，。！？,.!? \t") or text.strip()


def _image_quality_from_text(text: str) -> str:
    lowered = text.lower()
    if any(w in lowered for w in ("草图", "快速", "低清", "low")):
        return "low"
    if any(w in lowered for w in ("高清", "高质量", "精修", "正式", "high", "2k")):
        return "high"
    return "medium"


def _aspect_ratio_from_text(text: str) -> str:
    lowered = text.lower()
    if any(w in lowered for w in ("竖版", "海报", "手机", "9:16", "portrait")):
        return "portrait"
    if any(w in lowered for w in ("方图", "方形", "头像", "1:1", "square")):
        return "square"
    return "landscape"


def _dreamina_ratio_from_aspect(aspect_ratio: str) -> str:
    return {"portrait": "9:16", "square": "1:1"}.get(aspect_ratio, "16:9")


def _check_dreamina_status(output: str) -> tuple[bool, str]:
    try:
        json_match = re.search(r'\{.*"gen_status".*\}', output, re.DOTALL)
        if json_match:
            data = json.loads_safe(json_match.group())  # type: ignore[attr-defined]
            if data.get("gen_status") == "fail":
                return False, str(data.get("fail_reason") or "未知原因")
    except Exception:  # noqa: BLE001
        pass
    return True, ""


def _extract_url(output: str, suffixes: tuple[str, ...]) -> str | None:
    suffix_pattern = "|".join(re.escape(s.lstrip(".")) for s in suffixes)
    match = re.search(rf'https?://[^\s<>"]+\.(?:{suffix_pattern})[^\s<>"]*', output)
    return match.group() if match else None


async def _first_image_path(event: Any) -> str | None:
    try:
        message = event.message_obj.message
    except Exception:  # noqa: BLE001
        return None
    for comp in message:
        if isinstance(comp, ImageComp):
            try:
                path = await comp.convert_to_file_path()
                return path if path else None
            except Exception as exc:  # noqa: BLE001
                logger.warning("[dc_router] 获取图片附件失败: %s", exc)
                return None
    return None


def _has_image_attachment(event: Any) -> bool:
    try:
        message = event.message_obj.message
    except Exception:  # noqa: BLE001
        return False
    return any(isinstance(comp, ImageComp) for comp in message)


def is_source_image_edit_request(event: Any, text: str) -> bool:
    if not _SOURCE_IMAGE_EDIT_RE.search(text):
        return False
    return _has_image_attachment(event) or bool(_SOURCE_IMAGE_REF_RE.search(text))


def _event_sender_id(event: Any) -> str:
    getter = getattr(event, "get_sender_id", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "")
    except Exception:  # noqa: BLE001
        return ""


def _event_chat_id(event: Any) -> str:
    try:
        raw_message = getattr(event.message_obj, "raw_message", None)
    except Exception:  # noqa: BLE001
        raw_message = None
    for attr in ("chat_id", "p2p_chat_id", "open_chat_id"):
        value = getattr(raw_message, attr, "") if raw_message is not None else ""
        if value:
            return str(value)
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        try:
            group_id = str(getter() or "")
        except Exception:  # noqa: BLE001
            group_id = ""
        if group_id:
            return group_id
    return ""


def _source_image_edit_session_keys(event: Any) -> tuple[str, ...]:
    platform_id = _event_platform_id(event)
    keys = [
        str(getattr(event, "unified_msg_origin", "") or ""),
        f"chat:{platform_id}:{_event_chat_id(event)}",
        f"sender:{platform_id}:{_event_sender_id(event)}",
    ]
    return tuple(dict.fromkeys(key for key in keys if key and not key.endswith(":")))


def _remember_source_image_edit(
    event: Any,
    *,
    source_path: str,
    output_path: str,
    engine: str,
) -> None:
    session_keys = _source_image_edit_session_keys(event)
    if not session_keys:
        return
    state = {
        "source_path": source_path,
        "output_path": output_path,
        "engine": engine,
        "updated_at": time.time(),
    }
    for session_key in session_keys:
        _LAST_SOURCE_IMAGE_EDIT_BY_SESSION[session_key] = state


def _last_source_image_edit(event: Any) -> dict[str, Any] | None:
    for session_key in _source_image_edit_session_keys(event):
        state = _LAST_SOURCE_IMAGE_EDIT_BY_SESSION.get(session_key)
        if not state:
            continue
        updated_at = float(state.get("updated_at") or 0)
        source_path = str(state.get("source_path") or "")
        if time.time() - updated_at > _SOURCE_IMAGE_EDIT_STATE_TTL_SEC:
            _LAST_SOURCE_IMAGE_EDIT_BY_SESSION.pop(session_key, None)
            continue
        if not source_path or not Path(source_path).exists():
            _LAST_SOURCE_IMAGE_EDIT_BY_SESSION.pop(session_key, None)
            continue
        return state
    return None


def is_source_image_edit_followup(event: Any, text: str) -> bool:
    if _has_image_attachment(event):
        return False
    if not _SOURCE_IMAGE_EDIT_REFINEMENT_RE.search(text):
        return False
    return _last_source_image_edit(event) is not None


async def try_handle_source_image_edit(context: Any, event: Any, text: str) -> bool:
    """Handle deterministic source-image edits before LLM/routing stages."""
    stripped = text.strip()
    is_followup = is_source_image_edit_followup(event, stripped)
    if not is_followup and not is_source_image_edit_request(event, stripped):
        return False
    try:
        event.set_extra(
            "dc_media_route_handled",
            "source_image_edit_followup" if is_followup else "source_image_edit",
        )
        event.should_call_llm(False)
        if is_followup and not _SOURCE_IMAGE_EDIT_REPROCESS_RE.search(stripped):
            event.set_result(_build_source_image_edit_followup_prompt())
            return True
        event.set_result(
            await _build_source_image_edit_result(
                context, event, stripped, refine=is_followup
            )
        )
    except Exception:  # noqa: BLE001
        return False
    logger.info(
        "[dc_router] source image edit handled before generation prompt=%r session=%s",
        stripped[:80],
        event.unified_msg_origin,
    )
    return True


def _source_image_edit_unsupported_message(text: str) -> str:
    if _has_any_image_hint(text):
        return (
            "我识别到这是去背景/抠图请求，但没有取得可处理的图片文件。"
            "请把图片和文字放在同一条消息里重发；我不会把它当成生图任务。"
        )
    return (
        "这是原图编辑需求，但当前小助手还没有接通“去背景/抠图”工具。"
        "请先发送需要处理的图片；接通编辑工具前，我不会把它当成生图任务。"
    )


def _build_source_image_edit_followup_prompt() -> MessageEventResult:
    return (
        MessageEventResult()
        .message(
            "我识别到你是在反馈上一张抠图结果，不会把这句话送去排队或深度任务。\n\n"
            "当前已完成的是固定抠图 skill（rembg），它能稳定去背景，但发丝/毛边属于精修能力。"
            "如果要我继续处理，请回复「执行精修」；我会用 alpha-matting 再跑一次。"
            "如果精修后仍不理想，就需要接入更高阶抠图模型或人工修边。"
        )
        .use_t2i(False)
        .stop_event()
    )


def _has_any_image_hint(text: str) -> bool:
    return bool(_SOURCE_IMAGE_REF_RE.search(text))


def _median_rgb(samples: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    if not samples:
        return (255, 255, 255)
    samples_sorted = sorted(samples)
    mid = len(samples_sorted) // 2
    return (
        sorted(pixel[0] for pixel in samples)[mid],
        sorted(pixel[1] for pixel in samples)[mid],
        sorted(pixel[2] for pixel in samples)[mid],
    )


def _rgb_distance(
    pixel: tuple[int, int, int], background: tuple[int, int, int]
) -> float:
    return math.sqrt(
        (pixel[0] - background[0]) ** 2
        + (pixel[1] - background[1]) ** 2
        + (pixel[2] - background[2]) ** 2
    )


def _remove_background_with_rembg(source_path: str, *, refine: bool = False) -> str:
    try:
        from rembg import remove
    except BaseException as exc:  # noqa: BLE001
        raise RuntimeError("rembg is not installed") from exc

    source = Path(source_path)
    output_path = HERMES_CACHE_DIR / f"source_image_cutout_{uuid.uuid4().hex}.png"
    HERMES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if refine:
        output_path.write_bytes(
            remove(
                source.read_bytes(),
                alpha_matting=True,
                alpha_matting_foreground_threshold=240,
                alpha_matting_background_threshold=10,
                alpha_matting_erode_size=8,
            )
        )
    else:
        output_path.write_bytes(remove(source.read_bytes()))
    return str(output_path)


def _remove_connected_background_to_png(source_path: str) -> str:
    from PIL import Image, ImageFilter

    with Image.open(source_path) as opened:
        image = opened.convert("RGBA")

    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("图片尺寸无效，无法去背景。")

    pixels = list(image.getdata())
    border_samples: list[tuple[int, int, int]] = []
    for x in range(width):
        for y in (0, height - 1):
            red, green, blue, alpha = pixels[y * width + x]
            if alpha > 8:
                border_samples.append((red, green, blue))
    for y in range(height):
        for x in (0, width - 1):
            red, green, blue, alpha = pixels[y * width + x]
            if alpha > 8:
                border_samples.append((red, green, blue))

    background = _median_rgb(border_samples)
    border_distances = [
        _rgb_distance(sample, background)
        for sample in border_samples[:: max(1, len(border_samples) // 2000)]
    ]
    border_distances.sort()
    p90 = border_distances[int(len(border_distances) * 0.9)] if border_distances else 0
    threshold = min(55.0, max(18.0, p90 + 10.0))
    soft_threshold = threshold + 12.0

    candidate = bytearray(width * height)
    for index, (red, green, blue, alpha) in enumerate(pixels):
        if alpha <= 8:
            candidate[index] = 1
            continue
        if _rgb_distance((red, green, blue), background) <= soft_threshold:
            candidate[index] = 1

    background_mask = bytearray(width * height)
    queue: deque[int] = deque()

    def add_seed(index: int) -> None:
        if candidate[index] and not background_mask[index]:
            background_mask[index] = 1
            queue.append(index)

    for x in range(width):
        add_seed(x)
        add_seed((height - 1) * width + x)
    for y in range(height):
        add_seed(y * width)
        add_seed(y * width + width - 1)

    while queue:
        index = queue.popleft()
        x = index % width
        y = index // width
        if x > 0:
            neighbor = index - 1
            if candidate[neighbor] and not background_mask[neighbor]:
                background_mask[neighbor] = 1
                queue.append(neighbor)
        if x + 1 < width:
            neighbor = index + 1
            if candidate[neighbor] and not background_mask[neighbor]:
                background_mask[neighbor] = 1
                queue.append(neighbor)
        if y > 0:
            neighbor = index - width
            if candidate[neighbor] and not background_mask[neighbor]:
                background_mask[neighbor] = 1
                queue.append(neighbor)
        if y + 1 < height:
            neighbor = index + width
            if candidate[neighbor] and not background_mask[neighbor]:
                background_mask[neighbor] = 1
                queue.append(neighbor)

    alpha_values = bytearray(width * height)
    removed = 0
    for index, (red, green, blue, alpha) in enumerate(pixels):
        if not background_mask[index]:
            alpha_values[index] = alpha
            continue
        removed += 1
        distance = _rgb_distance((red, green, blue), background)
        if distance <= threshold:
            alpha_values[index] = 0
        else:
            alpha_values[index] = alpha

    if removed < max(16, int(width * height * 0.02)):
        raise ValueError("未检测到足够的连通背景区域，已停止输出以避免误抠。")
    if removed > int(width * height * 0.9):
        raise ValueError("自动去背景置信度低，为避免改动主体已停止输出。")

    alpha_mask = Image.frombytes("L", (width, height), bytes(alpha_values))
    alpha_mask = alpha_mask.filter(ImageFilter.GaussianBlur(radius=0.35))
    image.putalpha(alpha_mask)

    HERMES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    output_path = HERMES_CACHE_DIR / f"source_image_cutout_{uuid.uuid4().hex}.png"
    image.save(output_path)
    return str(output_path)


def _remove_background_with_skill(
    source_path: str, *, refine: bool = False
) -> tuple[str, str]:
    try:
        engine = "rembg alpha-matting" if refine else "rembg"
        return _remove_background_with_rembg(source_path, refine=refine), engine
    except BaseException as exc:  # noqa: BLE001
        logger.debug("[dc_router] rembg cutout unavailable, fallback to PIL: %s", exc)
    return _remove_connected_background_to_png(source_path), "PIL conservative"


async def _start_source_image_edit_card(context: Any, event: Any, text: str) -> Any:
    try:
        from dc_engines.card_runtime import send_card_via_runtime
        from dc_engines.feishu_card_streamer import (
            build_source_image_edit_card,
            ensure_streamers_on_context,
            extract_chat_info_from_event,
        )
    except Exception:  # noqa: BLE001
        return None

    platform_id = event.get_platform_id() or ""
    streamer = ensure_streamers_on_context(context).get(platform_id)
    if streamer is None:
        return None
    chat_id, receive_id_type = extract_chat_info_from_event(event)
    if not chat_id:
        return None
    card = build_source_image_edit_card(
        task_title="去背景任务",
        status="处理中",
        operation="去背景/人物抠出",
        source_summary="固定抠图 skill：只处理透明通道，人物/商品像素不交给生图模型重绘。",
    )
    stream = await send_card_via_runtime(
        streamer,
        card_type="source_image_edit",
        chat_id=chat_id,
        receive_id_type=receive_id_type,
        card=card,
        platform_id=platform_id,
        event="start",
        detail=f"source image edit started: {text[:80]}",
    )
    if stream is None:
        return None
    return {
        "streamer": streamer,
        "message_id": stream.message_id,
        "platform_id": platform_id,
        "started_at": time.time(),
    }


async def _finalize_source_image_edit_card(
    card: Any,
    *,
    success: bool,
    engine: str,
    output_path: str = "",
    error_hint: str = "",
) -> None:
    if card is None:
        return
    try:
        from dc_engines.card_runtime import finalize_card_via_runtime
        from dc_engines.feishu_card_streamer import build_source_image_edit_card
    except Exception:  # noqa: BLE001
        return
    elapsed_sec = time.time() - float(card.get("started_at") or time.time())
    final_card = build_source_image_edit_card(
        task_title="去背景任务",
        status="已完成" if success else "失败",
        operation="去背景/人物抠出",
        engine=engine,
        source_summary="固定抠图 skill：未进入生图/文案模型，不会重绘人物或商品。",
        output_hint=output_path if success else "",
        error_hint=error_hint,
        elapsed_sec=elapsed_sec,
    )
    await finalize_card_via_runtime(
        card["streamer"],
        card_type="source_image_edit",
        message_id=card["message_id"],
        card=final_card,
        platform_id=str(card.get("platform_id") or ""),
        detail="source image edit finalized",
    )


async def _build_source_image_edit_result(
    context: Any, event: Any, text: str, *, refine: bool = False
) -> MessageEventResult:
    image_path = await _first_image_path(event)
    if not image_path and refine:
        state = _last_source_image_edit(event)
        image_path = str((state or {}).get("source_path") or "")
    if not image_path:
        return (
            MessageEventResult()
            .message(_source_image_edit_unsupported_message(text))
            .use_t2i(False)
            .stop_event()
        )

    card = await _start_source_image_edit_card(context, event, text)
    try:
        output_path, engine = await asyncio.to_thread(
            _remove_background_with_skill, image_path, refine=refine
        )
    except Exception as exc:  # noqa: BLE001
        await _finalize_source_image_edit_card(
            card,
            success=False,
            engine="PIL conservative",
            error_hint=str(exc),
        )
        return (
            MessageEventResult()
            .message(f"去背景处理失败：{exc}")
            .use_t2i(False)
            .stop_event()
        )
    _remember_source_image_edit(
        event,
        source_path=image_path,
        output_path=output_path,
        engine=engine,
    )
    await _finalize_source_image_edit_card(
        card,
        success=True,
        engine=engine,
        output_path=output_path,
    )
    return (
        MessageEventResult()
        .file_image(output_path)
        .message(
            "已按上一张原图重新精修发丝/边缘，并导出透明 PNG。"
            if refine
            else "已去掉背景并导出透明 PNG。"
        )
        .use_t2i(False)
        .stop_event()
    )


async def _detect_route(event: Any, text: str) -> MediaRoute | None:
    stripped = text.strip()
    if is_source_image_edit_request(event, stripped):
        return None
    image_path = await _first_image_path(event)
    session_id = event.unified_msg_origin or ""
    if _is_plan_context_without_explicit_media_generation(stripped):
        return None
    if _IMAGE_VIDEO_TRIGGER_RE.search(stripped):
        route_image_path = image_path or _LAST_IMAGE_BY_SESSION.get(session_id)
        if route_image_path:
            return MediaRoute(
                kind="image2video",
                prompt=_extract_generation_prompt(stripped, intent="image2video"),
                image_path=route_image_path,
            )
    if _TEXT_VIDEO_TRIGGER_RE.search(stripped):
        return MediaRoute(
            kind="text2video",
            prompt=_extract_generation_prompt(stripped, intent="video"),
        )
    if _IMAGE_TRIGGER_RE.search(stripped):
        return MediaRoute(
            kind="image",
            prompt=_extract_generation_prompt(stripped, intent="image"),
            quality=_image_quality_from_text(stripped),
            aspect_ratio=_aspect_ratio_from_text(stripped),
            image_provider_strategy=_image_provider_strategy_from_text(stripped),
        )
    return None


def _is_plan_context_without_explicit_media_generation(text: str) -> bool:
    if not _PLAN_CONTEXT_RE.search(text):
        return False
    if _TEXT_VIDEO_TRIGGER_RE.search(text) or _IMAGE_VIDEO_TRIGGER_RE.search(text):
        return False
    return not _EXPLICIT_IMAGE_REQUEST_RE.search(text)


def _image_provider_strategy_from_text(text: str) -> ImageProviderStrategy:
    if _DREAMINA_FIRST_RE.search(text):
        return "dreamina_first"
    return "gpt_first"


def _media_task_title(route: MediaRoute) -> str:
    return {
        "image": "生图任务",
        "image2video": "图片转视频",
        "text2video": "文生视频",
    }.get(route.kind, "媒体生成")


def _image_waiting_stage(route: MediaRoute) -> str:
    if route.image_provider_strategy == "dreamina_first":
        return "Dreamina 即梦正在生成中文营销视觉，失败会自动切 GPT Image 2"
    return "GPT Image 2 正在生成英文营销视觉指令，失败会自动切 Dreamina"


def _image_engine_label(route: MediaRoute) -> str:
    if route.image_provider_strategy == "dreamina_first":
        return "Dreamina 即梦 / GPT Image 2"
    return "GPT Image 2 / Dreamina"


async def _start_waiting_card(context: Any, event: Any, route: MediaRoute) -> Any:
    """Start a waiting card via dc_engines.feishu_card_streamer — best effort."""
    try:
        from dc_engines.feishu_card_streamer import start_waiting_card_for_event
    except Exception:  # noqa: BLE001
        return None
    return await start_waiting_card_for_event(
        context,
        event,
        title=_media_task_title(route),
        brief=route.prompt or _media_task_title(route),
        reasoning_tier="high" if route.kind == "image" else "xhigh",
        current_stage={
            "image": _image_waiting_stage(route),
            "image2video": "Dreamina 正在把静态图动画化",
            "text2video": "Dreamina 正在生成视频",
        }.get(route.kind, "媒体任务处理中"),
        interval_sec=5.0,
    )


def _route_to_dict(route: MediaRoute) -> dict[str, Any]:
    return {
        "kind": route.kind,
        "prompt": route.prompt,
        "image_path": route.image_path,
        "quality": route.quality,
        "aspect_ratio": route.aspect_ratio,
        "image_provider_strategy": route.image_provider_strategy,
    }


def _route_from_dict(data: dict[str, Any]) -> MediaRoute | None:
    kind = data.get("kind")
    if kind not in {"image", "image2video", "text2video"}:
        return None
    strategy = data.get("image_provider_strategy")
    if strategy not in {"gpt_first", "dreamina_first"}:
        strategy = "gpt_first"
    return MediaRoute(
        kind=kind,
        prompt=str(data.get("prompt") or ""),
        image_path=data.get("image_path") if data.get("image_path") else None,
        quality=str(data.get("quality") or "medium"),
        aspect_ratio=str(data.get("aspect_ratio") or "landscape"),
        image_provider_strategy=strategy,
    )


def _load_pending_media_tasks() -> list[dict[str, Any]]:
    try:
        if not _MEDIA_TASKS_PATH.exists():
            return []
        data = json.loads(_MEDIA_TASKS_PATH.read_text(encoding="utf-8") or "[]")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] 读取媒体任务队列失败: %s", exc)
    return []


def _save_pending_media_tasks(tasks: list[dict[str, Any]]) -> None:
    _MEDIA_TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _MEDIA_TASKS_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(tasks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(_MEDIA_TASKS_PATH)


def _upsert_pending_media_task(record: dict[str, Any]) -> None:
    tasks = [
        item
        for item in _load_pending_media_tasks()
        if item.get("task_id") != record.get("task_id")
    ]
    tasks.append(record)
    _save_pending_media_tasks(tasks)


def _remove_pending_media_task(task_id: str) -> None:
    if not task_id:
        return
    tasks = [
        item for item in _load_pending_media_tasks() if item.get("task_id") != task_id
    ]
    _save_pending_media_tasks(tasks)


def _event_platform_id(event: Any) -> str:
    get_platform_id = getattr(event, "get_platform_id", None)
    if callable(get_platform_id):
        return str(get_platform_id() or "")
    return ""


def _pending_record_for_route(
    event: Any,
    route: MediaRoute,
    card: Any,
    *,
    task_id: str,
) -> dict[str, Any]:
    now = time.time()
    return {
        "task_id": task_id,
        "created_at": now,
        "updated_at": now,
        "umo": str(getattr(event, "unified_msg_origin", "") or ""),
        "platform_id": _event_platform_id(event),
        "route": _route_to_dict(route),
        "card": {
            "message_id": str(getattr(card, "message_id", "") or ""),
            "chat_id": str(getattr(card, "chat_id", "") or ""),
            "receive_id_type": str(getattr(card, "receive_id_type", "") or ""),
            "title": str(getattr(card, "title", "") or _media_task_title(route)),
            "brief": str(getattr(card, "brief", "") or route.prompt),
            "reasoning_tier": getattr(card, "reasoning_tier", None),
            "current_stage": getattr(card, "current_stage", None),
        },
    }


def _restore_waiting_card(
    context: Any, record: dict[str, Any], route: MediaRoute
) -> Any:
    card_info = record.get("card") if isinstance(record.get("card"), dict) else {}
    message_id = str(card_info.get("message_id") or "")
    platform_id = str(record.get("platform_id") or "")
    if not message_id or not platform_id:
        return None
    try:
        from dc_engines.feishu_card_streamer import (
            WaitingCardHandle,
            ensure_streamers_on_context,
        )
        from dc_engines.feishu_card_streamer.streamer import CardStream
    except Exception:  # noqa: BLE001
        return None

    streamer = ensure_streamers_on_context(context).get(platform_id)
    if streamer is None:
        return None
    chat_id = str(card_info.get("chat_id") or "")
    receive_id_type = str(card_info.get("receive_id_type") or "")
    if not chat_id or not receive_id_type:
        return None
    stream = CardStream(
        message_id=message_id,
        chat_id=chat_id,
        receive_id_type=receive_id_type,
        created_at=float(record.get("created_at") or time.time()),
    )
    streamer._streams[message_id] = stream  # noqa: SLF001
    handle = WaitingCardHandle(
        streamer=streamer,
        message_id=message_id,
        chat_id=chat_id,
        receive_id_type=receive_id_type,
        title=str(card_info.get("title") or _media_task_title(route)),
        brief=str(card_info.get("brief") or route.prompt)[:200],
        reasoning_tier=card_info.get("reasoning_tier"),
        current_stage="系统刚重启，正在恢复媒体生成任务",
    )

    def _builder(s):
        return handle.build_card(s.elapsed_sec)

    streamer.start_auto_update(message_id, _builder, interval_sec=5.0)
    return handle


async def _finalize_waiting_card(
    context: Any,
    card: Any,
    *,
    route: MediaRoute,
    success: bool,
    detail: str,
    output_url: str = "",
    output_path: str = "",
    cancelled: bool = False,
) -> bool:
    if card is None:
        return False
    try:
        from dc_engines.card_runtime import finalize_card_via_runtime
        from dc_engines.media_sop import (
            build_media_generation_card,
            build_media_generation_record,
        )
    except Exception:  # noqa: BLE001
        return False
    stream = card.streamer.get_stream(card.message_id)
    elapsed_sec = stream.elapsed_sec if stream else 0
    media_kind = (
        "image"
        if route.kind == "image"
        else ("image2video" if route.kind == "image2video" else "video")
    )
    engine = _image_engine_label(route) if route.kind == "image" else "Dreamina 即梦"
    record = build_media_generation_record(
        media_kind=media_kind,
        prompt=route.prompt,
        engine=engine,
        status="cancelled" if cancelled else ("succeeded" if success else "failed"),
        aspect_ratio=route.aspect_ratio,
        output_url=output_url,
        output_path=output_path,
        error_hint="" if success else detail,
    )
    final_card = build_media_generation_card(
        task_title=_media_task_title(route),
        media_type=media_kind,
        status="已取消" if cancelled else ("已完成" if success else "失败"),
        prompt=record.to_card_detail(),
        engine=engine,
        task_id=record.record_id,
        aspect_ratio=route.aspect_ratio,
        output_url=output_path or output_url or detail,
        error_hint="" if success else detail,
        elapsed_sec=elapsed_sec,
    )
    return await finalize_card_via_runtime(
        card.streamer,
        card_type="media_generation",
        message_id=card.message_id,
        card=final_card,
        platform_id="",
        detail=f"dc_router media finalized: {route.kind} record={record.record_id}",
        retract_after_sec=8.0 if success else None,
    )


async def _send_chain(umo: str, chain: MessageChain) -> None:
    try:
        pass
        # Caller supplies context — handled below
    except Exception:  # noqa: BLE001
        pass


async def _run_dreamina_command(
    command: list[str], *, timeout: int, retries: int = 3
) -> tuple[bool, str]:
    dreamina_bin = resolve_dreamina_executable()
    if dreamina_bin is None:
        return False, dreamina_command_not_found_message()

    for attempt in range(1, retries + 1):
        try:
            proc = await asyncio.create_subprocess_exec(
                dreamina_bin,
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(project_root()),
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except FileNotFoundError:
            return False, dreamina_command_not_found_message()
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:  # noqa: BLE001
                pass
            return False, f"Dreamina 执行超时（{timeout}s）"
        except Exception as exc:  # noqa: BLE001
            return False, f"Dreamina 执行异常: {exc}"

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        output = stdout + (f"\n错误：{stderr}" if stderr else "")
        if "ExceedConcurrencyLimit" in output and attempt < retries:
            await asyncio.sleep(10 * attempt)
            continue
        return proc.returncode == 0, output or f"Dreamina 返回码 {proc.returncode}"
    return False, "多次重试后仍触发 Dreamina 并发限制，请稍后再试"


async def _download_url_to_cache(url: str, *, suffix: str) -> str:
    HERMES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = HERMES_CACHE_DIR / f"dreamina_media_{abs(hash(url))}{suffix}"
    await asyncio.to_thread(urllib.request.urlretrieve, url, str(target))
    return str(target)


async def _run_image_job(
    context: Any,
    umo: str,
    route: MediaRoute,
    card: Any,
) -> None:
    module = _load_gpt_image_module()
    try:
        from dc_engines.media_sop import build_structured_media_prompt
    except Exception:  # noqa: BLE001
        return
    image2_prompt = build_structured_media_prompt(
        route.prompt,
        media_kind="image",
        aspect_ratio=route.aspect_ratio,
        target_engine="gpt-image-2",
    )
    dreamina_prompt = build_structured_media_prompt(
        route.prompt,
        media_kind="image",
        aspect_ratio=route.aspect_ratio,
        target_engine="dreamina",
    )
    loop = asyncio.get_running_loop()
    if route.image_provider_strategy == "dreamina_first":
        success, result, provider_label = await _run_image_dreamina_first(
            loop,
            module,
            image2_prompt=image2_prompt,
            dreamina_prompt=dreamina_prompt,
            route=route,
        )
    else:
        success, result, provider_label = await _run_image_gpt_first(
            context,
            umo,
            loop,
            module,
            image2_prompt=image2_prompt,
            dreamina_prompt=dreamina_prompt,
            route=route,
            card=card,
        )
    if not success:
        if not await _finalize_waiting_card(
            context, card, route=route, success=False, detail=result
        ):
            try:
                await context.send_message(umo, MessageChain([Plain(result)]))
            except Exception:  # noqa: BLE001
                pass
        return

    _LAST_IMAGE_BY_SESSION[umo] = result
    await _finalize_waiting_card(
        context,
        card,
        route=route,
        success=True,
        detail=f"图片已生成，会在下一条消息里发送（{provider_label}）。",
        output_path=result,
    )
    try:
        await context.send_message(
            umo,
            MessageChain(
                [
                    ImageComp.fromFileSystem(result),
                    Plain(f"已生成（{provider_label}）。"),
                ]
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] 发送图片失败: %s", exc)


async def _run_image_gpt_first(
    context: Any,
    umo: str,
    loop: asyncio.AbstractEventLoop,
    module: Any,
    *,
    image2_prompt: str,
    dreamina_prompt: str,
    route: MediaRoute,
    card: Any,
) -> tuple[bool, str, str]:
    success, result = await loop.run_in_executor(
        None,
        module._call_codex_image_gen,
        image2_prompt,
        route.quality,
        route.aspect_ratio,
    )
    if success:
        return True, result, f"GPT Image 2 · {route.quality}"

    gpt_error = result
    if card is None:
        try:
            await context.send_message(
                umo,
                MessageChain(
                    [
                        Plain(
                            "GPT Image 2 暂时不可用，已自动切换 Dreamina 即梦继续生图。"
                        )
                    ]
                ),
            )
        except Exception:  # noqa: BLE001
            pass
    success, result = await loop.run_in_executor(
        None,
        module._dreamina_text2image_sync,
        dreamina_prompt,
        route.aspect_ratio,
    )
    if success:
        return True, result, "Dreamina 即梦 · 自动兜底"
    return False, f"生图失败。\nGPT Image 2: {gpt_error}\nDreamina: {result}", ""


async def _run_image_dreamina_first(
    loop: asyncio.AbstractEventLoop,
    module: Any,
    *,
    image2_prompt: str,
    dreamina_prompt: str,
    route: MediaRoute,
) -> tuple[bool, str, str]:
    success, result = await loop.run_in_executor(
        None,
        module._dreamina_text2image_sync,
        dreamina_prompt,
        route.aspect_ratio,
    )
    if success:
        return True, result, "Dreamina 即梦 · 中文营销视觉优先"

    dreamina_error = result
    success, result = await loop.run_in_executor(
        None,
        module._call_codex_image_gen,
        image2_prompt,
        route.quality,
        route.aspect_ratio,
    )
    if success:
        return True, result, f"GPT Image 2 · 兜底 · {route.quality}"
    return False, f"生图失败。\nDreamina: {dreamina_error}\nGPT Image 2: {result}", ""


async def _run_video_job(
    context: Any,
    umo: str,
    route: MediaRoute,
    card: Any,
) -> None:
    try:
        from dc_engines.media_sop import build_structured_media_prompt
    except Exception:  # noqa: BLE001
        return
    if route.kind == "image2video":
        if not route.image_path:
            msg = "没有找到可动画化的静态图片。"
            if not await _finalize_waiting_card(
                context, card, route=route, success=False, detail=msg
            ):
                try:
                    await context.send_message(umo, MessageChain([Plain(msg)]))
                except Exception:  # noqa: BLE001
                    pass
            return
        command = [
            "image2video",
            "--image",
            route.image_path,
            "--prompt",
            build_structured_media_prompt(
                route.prompt or "animate the scene",
                media_kind="image2video",
                aspect_ratio=route.aspect_ratio,
            ),
            "--duration",
            "5",
            "--poll",
            "900",
        ]
        label = "图片转视频"
    else:
        command = [
            "text2video",
            "--prompt",
            build_structured_media_prompt(
                route.prompt, media_kind="video", aspect_ratio=route.aspect_ratio
            ),
            "--duration",
            "5",
            "--ratio",
            _dreamina_ratio_from_aspect(route.aspect_ratio),
            "--video_resolution",
            "720p",
            "--poll",
            "900",
        ]
        label = "文生视频"
    success, output = await _run_dreamina_command(command, timeout=900)
    if not success:
        msg = f"{label}失败：{output}"
        if not await _finalize_waiting_card(
            context, card, route=route, success=False, detail=msg
        ):
            try:
                await context.send_message(umo, MessageChain([Plain(msg)]))
            except Exception:  # noqa: BLE001
                pass
        return
    ok, fail_reason = _check_dreamina_status(output)
    if not ok:
        msg = f"{label}失败：{fail_reason}"
        if not await _finalize_waiting_card(
            context, card, route=route, success=False, detail=msg
        ):
            try:
                await context.send_message(umo, MessageChain([Plain(msg)]))
            except Exception:  # noqa: BLE001
                pass
        return
    video_url = _extract_url(output, (".mp4",))
    if not video_url:
        msg = f"{label}完成，但未解析到 mp4 链接：\n{output[:800]}"
        if not await _finalize_waiting_card(
            context, card, route=route, success=False, detail=msg
        ):
            try:
                await context.send_message(umo, MessageChain([Plain(msg)]))
            except Exception:  # noqa: BLE001
                pass
        return
    local_video = ""
    try:
        local_video = await _download_url_to_cache(video_url, suffix=".mp4")
        chain = MessageChain(
            [
                VideoComp.fromFileSystem(local_video),
                Plain(f"{label}完成（Dreamina 即梦）。"),
            ]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] 下载 Dreamina 视频失败: %s", exc)
        chain = MessageChain([Plain(f"{label}完成：{video_url}")])
    await _finalize_waiting_card(
        context,
        card,
        route=route,
        success=True,
        detail=f"{label}已完成，会在下一条消息里发送。",
        output_url=video_url,
        output_path=local_video,
    )
    try:
        await context.send_message(umo, chain)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] 发送视频失败: %s", exc)


async def _background_job(context: Any, umo: str, route: MediaRoute, card: Any) -> None:
    try:
        if route.kind == "image":
            await _run_image_job(context, umo, route, card)
        else:
            await _run_video_job(context, umo, route, card)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] media route failed kind=%s: %s", route.kind, exc)
        msg = f"媒体生成任务失败：{exc}"
        if not await _finalize_waiting_card(
            context, card, route=route, success=False, detail=msg
        ):
            try:
                await context.send_message(umo, MessageChain([Plain(msg)]))
            except Exception:  # noqa: BLE001
                pass


async def _background_job_with_record(
    context: Any,
    umo: str,
    route: MediaRoute,
    card: Any,
    task_id: str,
) -> None:
    try:
        await _background_job(context, umo, route, card)
    finally:
        _ACTIVE_MEDIA_TASKS.pop(task_id, None)
        _ACTIVE_MEDIA_CONTEXT.pop(task_id, None)
        _remove_pending_media_task(task_id)


async def cancel_session_media_tasks(
    context: Any,
    umo: str,
    *,
    reason: str,
) -> int:
    """Cancel active and persisted media jobs for a session.

    Args:
        context: Shared runtime context used to finalize waiting cards.
        umo: Unified message origin identifying the session.
        reason: Cancellation reason shown on the final card.

    Returns:
        Number of media jobs removed from active or recovery state.
    """
    records = {
        str(item.get("task_id") or ""): item
        for item in _load_pending_media_tasks()
        if str(item.get("umo") or "") == umo and item.get("task_id")
    }
    task_ids = set(records)
    task_ids.update(
        task_id
        for task_id, (session_id, _route, _card) in _ACTIVE_MEDIA_CONTEXT.items()
        if session_id == umo
    )
    for task_id in task_ids:
        worker = _ACTIVE_MEDIA_TASKS.pop(task_id, None)
        active_context = _ACTIVE_MEDIA_CONTEXT.pop(task_id, None)
        if worker is not None:
            try:
                worker.cancel()
            except Exception:  # noqa: BLE001
                pass

        if active_context is not None:
            _session_id, route, card = active_context
        else:
            record = records.get(task_id) or {}
            route_data = record.get("route")
            route = (
                _route_from_dict(route_data) if isinstance(route_data, dict) else None
            )
            card = _restore_waiting_card(context, record, route) if route else None
        if route is not None and card is not None:
            await _finalize_waiting_card(
                context,
                card,
                route=route,
                success=False,
                detail=reason or "任务已取消",
                cancelled=True,
            )
        _remove_pending_media_task(task_id)
    return len(task_ids)


async def _resume_pending_media_tasks(context: Any) -> int:
    resumed = 0
    for record in _load_pending_media_tasks():
        task_id = str(record.get("task_id") or "")
        route_data = record.get("route")
        if not task_id or not isinstance(route_data, dict):
            continue
        route = _route_from_dict(route_data)
        if route is None:
            _remove_pending_media_task(task_id)
            continue
        umo = str(record.get("umo") or "")
        if not umo:
            _remove_pending_media_task(task_id)
            continue
        card = _restore_waiting_card(context, record, route)
        worker = asyncio.create_task(
            _background_job_with_record(context, umo, route, card, task_id)
        )
        _ACTIVE_MEDIA_TASKS[task_id] = worker
        _ACTIVE_MEDIA_CONTEXT[task_id] = (umo, route, card)
        resumed += 1
        logger.info(
            "[dc_router] restored media route task=%s kind=%s session=%s",
            task_id,
            route.kind,
            umo,
        )
    return resumed


async def _media_recovery_loop(context: Any, *, startup_delay_sec: float = 3.0) -> None:
    await asyncio.sleep(startup_delay_sec)
    try:
        resumed = await _resume_pending_media_tasks(context)
        if resumed:
            logger.info("[dc_router] restored %s pending media task(s)", resumed)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] media task recovery failed: %s", exc)


def start_media_task_recovery(context: Any) -> None:
    global _MEDIA_RECOVERY_TASK
    if _MEDIA_RECOVERY_TASK is not None and not _MEDIA_RECOVERY_TASK.done():
        return
    _MEDIA_RECOVERY_TASK = asyncio.create_task(_media_recovery_loop(context))
    logger.info("[dc_router] media_task_recovery 启动")


def stop_media_task_recovery() -> asyncio.Task | None:
    """Cancel the media recovery task and return it for awaited shutdown.

    Returns:
        The cancelled recovery task, or ``None`` when no task was active.
    """
    global _MEDIA_RECOVERY_TASK
    task = _MEDIA_RECOVERY_TASK
    if _MEDIA_RECOVERY_TASK is not None and not _MEDIA_RECOVERY_TASK.done():
        _MEDIA_RECOVERY_TASK.cancel()
    _MEDIA_RECOVERY_TASK = None
    return task


async def try_handle_media_route(context: Any, event: Any, text: str) -> bool:
    """True 表示已接管 (返回 ack 消息 + 派发后台任务)。"""
    stripped = text.strip()
    if await try_handle_source_image_edit(context, event, stripped):
        return True
    route = await _detect_route(event, text)
    if route is None:
        return False
    try:
        event.set_extra("dc_media_route_handled", route.kind)
        event.should_call_llm(False)
        card = await _start_waiting_card(context, event, route)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] start waiting card failed: %s", exc)
        return False
    ack = {
        "image": "已进入生图任务：GPT Image 2 主用，Dreamina 即梦自动兜底。",
        "image2video": "已进入图片转视频任务：Dreamina 即梦处理中。",
        "text2video": "已进入文生视频任务：Dreamina 即梦处理中。",
    }[route.kind]
    if card is not None:
        ack = f"{ack}\n等待卡会持续计时，完成后会自动更新。"
    task_id = uuid.uuid4().hex[:12]
    try:
        _upsert_pending_media_task(
            _pending_record_for_route(event, route, card, task_id=task_id)
        )
        result = MessageEventResult().use_t2i(False).stop_event()
        if card is None:
            result.message(ack)
        else:
            event.set_extra("dc_media_route_ack_suppressed", "1")
        event.set_result(result)
    except Exception:  # noqa: BLE001
        return False
    worker = asyncio.create_task(
        _background_job_with_record(
            context,
            event.unified_msg_origin,
            route,
            card,
            task_id,
        )
    )
    _ACTIVE_MEDIA_TASKS[task_id] = worker
    _ACTIVE_MEDIA_CONTEXT[task_id] = (
        event.unified_msg_origin,
        route,
        card,
    )
    logger.info(
        "[dc_router] media route kind=%s prompt=%r session=%s",
        route.kind,
        route.prompt[:80],
        event.unified_msg_origin,
    )
    return True


__all__ = [
    "MediaRoute",
    "cancel_session_media_tasks",
    "is_source_image_edit_request",
    "start_media_task_recovery",
    "stop_media_task_recovery",
    "try_handle_media_route",
    "try_handle_source_image_edit",
]
