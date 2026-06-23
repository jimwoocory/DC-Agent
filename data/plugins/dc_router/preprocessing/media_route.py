"""Media route — image / video / image2video generation.

检测 trigger regex → 选 provider → 走 GPT Image 2 / Dreamina CLI →
发等待卡 → 完成后发图片 / 视频。本模块不阻塞主消息（创建 asyncio task 跑后台）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import urllib.request
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

MediaRouteKind = Literal["image", "text2video", "image2video"]


@dataclass(frozen=True, slots=True)
class MediaRoute:
    kind: MediaRouteKind
    prompt: str
    image_path: str | None = None
    quality: str = "medium"
    aspect_ratio: str = "landscape"


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

_LAST_IMAGE_BY_SESSION: dict[str, str] = {}
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


async def _detect_route(event: Any, text: str) -> MediaRoute | None:
    stripped = text.strip()
    image_path = await _first_image_path(event)
    session_id = event.unified_msg_origin or ""
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
        )
    return None


def _media_task_title(route: MediaRoute) -> str:
    return {
        "image": "生图任务",
        "image2video": "图片转视频",
        "text2video": "文生视频",
    }.get(route.kind, "媒体生成")


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
            "image": "GPT Image 2 正在生成，失败会自动切 Dreamina",
            "image2video": "Dreamina 正在把静态图动画化",
            "text2video": "Dreamina 正在生成视频",
        }.get(route.kind, "媒体任务处理中"),
        interval_sec=5.0,
    )


async def _finalize_waiting_card(
    context: Any,
    card: Any,
    *,
    route: MediaRoute,
    success: bool,
    detail: str,
    output_url: str = "",
    output_path: str = "",
) -> bool:
    if card is None:
        return False
    try:
        from dc_engines.feishu_card_streamer import finalize_card_via_runtime
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
    engine = "GPT Image 2 / Dreamina" if route.kind == "image" else "Dreamina 即梦"
    record = build_media_generation_record(
        media_kind=media_kind,
        prompt=route.prompt,
        engine=engine,
        status="succeeded" if success else "failed",
        aspect_ratio=route.aspect_ratio,
        output_url=output_url,
        output_path=output_path,
        error_hint="" if success else detail,
    )
    final_card = build_media_generation_card(
        task_title=_media_task_title(route),
        media_type=media_kind,
        status="已完成" if success else "失败",
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


# json 安全加载 — 避免每个 helper 都 import json
import json  # noqa: E402


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
    structured_prompt = build_structured_media_prompt(
        route.prompt, media_kind="image", aspect_ratio=route.aspect_ratio
    )
    loop = asyncio.get_running_loop()
    success, result = await loop.run_in_executor(
        None,
        module._call_codex_image_gen,
        structured_prompt,
        route.quality,
        route.aspect_ratio,
    )
    provider_label = f"GPT Image 2 · {route.quality}"
    if not success:
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
            structured_prompt,
            route.aspect_ratio,
        )
        provider_label = "Dreamina 即梦 · 自动兜底"
        if not success:
            msg = f"生图失败。\nGPT Image 2: {gpt_error}\nDreamina: {result}"
            if not await _finalize_waiting_card(
                context, card, route=route, success=False, detail=msg
            ):
                try:
                    await context.send_message(umo, MessageChain([Plain(msg)]))
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


async def try_handle_media_route(context: Any, event: Any, text: str) -> bool:
    """True 表示已接管 (返回 ack 消息 + 派发后台任务)。"""
    route = await _detect_route(event, text)
    if route is None:
        return False
    try:
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
    try:
        event.set_result(MessageEventResult().message(ack).use_t2i(False).stop_event())
    except Exception:  # noqa: BLE001
        return False
    asyncio.create_task(_background_job(context, event.unified_msg_origin, route, card))
    logger.info(
        "[dc_router] media route kind=%s prompt=%r session=%s",
        route.kind,
        route.prompt[:80],
        event.unified_msg_origin,
    )
    return True


__all__ = ["MediaRoute", "try_handle_media_route"]
