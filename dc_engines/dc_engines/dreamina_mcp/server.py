"""MCP tools for Dreamina CLI media production workflows."""

from __future__ import annotations

import json
from typing import Literal

from mcp.server.fastmcp import FastMCP

from dc_engines.dreamina_mcp.runner import (
    normalize_ratio,
    result_json,
    run_dreamina,
    validate_existing_file,
    validate_existing_files,
)
from dc_engines.media_sop import build_structured_media_prompt

mcp = FastMCP("dreamina_mcp")

ImageRatio = Literal["21:9", "16:9", "3:2", "4:3", "1:1", "3:4", "2:3", "9:16"]
VideoRatio = Literal["1:1", "3:4", "16:9", "4:3", "9:16", "21:9"]
ImageResolution = Literal["1k", "2k", "4k"]
UpscaleResolution = Literal["2k", "4k", "8k"]
VideoResolution = Literal["720p", "1080p"]
ResponseFormat = Literal["json", "markdown"]


def _format_response(payload: dict, response_format: ResponseFormat) -> str:
    """Format a tool result for MCP clients.

    Args:
        payload: Structured tool payload.
        response_format: Desired response format.

    Returns:
        JSON or Markdown response text.
    """
    if response_format == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2)

    status = "succeeded" if payload.get("ok") else "failed"
    lines = [
        f"## Dreamina task {status}",
        f"- submit_id: `{payload.get('submit_id') or 'n/a'}`",
        f"- gen_status: `{payload.get('gen_status') or 'n/a'}`",
        f"- task_state: `{payload.get('task_state') or 'unknown'}`",
        f"- result_ready: `{payload.get('result_ready')}`",
        f"- output_dir: `{payload.get('output_dir')}`",
    ]
    if payload.get("next_action"):
        lines.append(f"- next_action: {payload['next_action']}")
    if payload.get("downloaded_files"):
        lines.append("- downloaded_files:")
        lines.extend(f"  - `{path}`" for path in payload["downloaded_files"])
    if payload.get("media_urls"):
        lines.append("- media_urls:")
        lines.extend(f"  - {url}" for url in payload["media_urls"])
    if payload.get("error_hint"):
        lines.append(f"- error_hint: {payload['error_hint']}")
    return "\n".join(lines)


async def _run_json(
    args: list[str],
    *,
    timeout: int,
    output_dir: str | None = None,
    download: bool = False,
    media_kind: Literal["image", "video", "image2video"] | None = None,
    prompt: str = "",
    response_format: ResponseFormat = "json",
) -> str:
    """Run Dreamina and serialize the result.

    Args:
        args: Dreamina CLI arguments.
        timeout: Subprocess timeout in seconds.
        output_dir: Optional result directory.
        download: Whether to download parsed media URLs.
        media_kind: Optional media kind for SOP records.
        prompt: Prompt used for media records.
        response_format: JSON or Markdown response format.

    Returns:
        Serialized MCP response.
    """
    result = await run_dreamina(
        args,
        timeout=timeout,
        output_dir=output_dir,
        download=download,
        media_kind=media_kind,
        prompt=prompt,
    )
    if response_format == "json":
        return result_json(result)
    return _format_response(result.to_dict(), response_format)


def _append_option(args: list[str], flag: str, value: object | None) -> None:
    """Append a CLI flag when a value is present.

    Args:
        args: Mutable CLI argument list.
        flag: Flag name including leading dashes.
        value: Optional value to append.
    """
    if value is not None and value != "":
        args.extend([flag, str(value)])


@mcp.tool(
    name="dreamina_health",
    annotations={
        "title": "Check Dreamina CLI availability",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def dreamina_health(response_format: ResponseFormat = "json") -> str:
    """Check that the local Dreamina CLI can be started.

    Args:
        response_format: ``json`` for structured data or ``markdown`` for a
            compact human-readable summary.

    Returns:
        Dreamina CLI version/help probe result.
    """
    return await _run_json(["version"], timeout=20, response_format=response_format)


@mcp.tool(
    name="dreamina_user_credit",
    annotations={
        "title": "Check Dreamina credits",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def dreamina_user_credit(response_format: ResponseFormat = "json") -> str:
    """Show the logged-in Dreamina account credit balance.

    Args:
        response_format: ``json`` or ``markdown``.

    Returns:
        Dreamina credit query result.
    """
    return await _run_json(["user_credit"], timeout=30, response_format=response_format)


@mcp.tool(
    name="dreamina_login_start",
    annotations={
        "title": "Start Dreamina OAuth login",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def dreamina_login_start(response_format: ResponseFormat = "json") -> str:
    """Start headless Dreamina OAuth Device Flow login.

    Args:
        response_format: ``json`` or ``markdown``.

    Returns:
        Login material containing verification URL, user code, and device code
        when Dreamina CLI emits them.
    """
    return await _run_json(
        ["login", "--headless"], timeout=60, response_format=response_format
    )


@mcp.tool(
    name="dreamina_login_check",
    annotations={
        "title": "Check Dreamina OAuth login",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def dreamina_login_check(
    device_code: str,
    poll_seconds: int = 30,
    response_format: ResponseFormat = "json",
) -> str:
    """Check a prior headless Dreamina login.

    Args:
        device_code: Device code printed by ``dreamina_login_start``.
        poll_seconds: Seconds to wait for authorization completion.
        response_format: ``json`` or ``markdown``.

    Returns:
        Login check result.
    """
    return await _run_json(
        [
            "login",
            "checklogin",
            "--device_code",
            device_code,
            "--poll",
            str(max(0, poll_seconds)),
        ],
        timeout=max(45, poll_seconds + 15),
        response_format=response_format,
    )


@mcp.tool(
    name="dreamina_list_tasks",
    annotations={
        "title": "List Dreamina tasks",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def dreamina_list_tasks(
    gen_status: str = "",
    gen_task_type: str = "",
    submit_id: str = "",
    limit: int = 20,
    offset: int = 0,
    response_format: ResponseFormat = "json",
) -> str:
    """List saved Dreamina tasks for the logged-in account.

    Args:
        gen_status: Optional Dreamina status filter.
        gen_task_type: Optional Dreamina task type filter.
        submit_id: Optional submit id filter.
        limit: Maximum number of tasks to return.
        offset: Pagination offset.
        response_format: ``json`` or ``markdown``.

    Returns:
        Task list result.
    """
    args = [
        "list_task",
        "--limit",
        str(max(1, min(limit, 100))),
        "--offset",
        str(max(0, offset)),
    ]
    _append_option(args, "--gen_status", gen_status)
    _append_option(args, "--gen_task_type", gen_task_type)
    _append_option(args, "--submit_id", submit_id)
    return await _run_json(args, timeout=45, response_format=response_format)


@mcp.tool(
    name="dreamina_query_result",
    annotations={
        "title": "Query Dreamina task result",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def dreamina_query_result(
    submit_id: str,
    download: bool = True,
    output_dir: str = "",
    response_format: ResponseFormat = "json",
) -> str:
    """Query one async Dreamina task and optionally download result media.

    Args:
        submit_id: Dreamina task submit id.
        download: Whether to download result media.
        output_dir: Optional result directory.
        response_format: ``json`` or ``markdown``.

    Returns:
        Query result and downloaded file paths when available.
    """
    args = ["query_result", "--submit_id", submit_id]
    if download:
        _append_option(args, "--download_dir", output_dir or None)
    return await _run_json(
        args,
        timeout=180,
        output_dir=output_dir or None,
        download=download,
        response_format=response_format,
    )


@mcp.tool(
    name="dreamina_text2image",
    annotations={
        "title": "Generate image from text",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def dreamina_text2image(
    prompt: str,
    ratio: ImageRatio = "1:1",
    resolution_type: ImageResolution = "2k",
    model_version: str = "",
    poll_seconds: int = 0,
    session: int = 0,
    download: bool = True,
    output_dir: str = "",
    response_format: ResponseFormat = "json",
) -> str:
    """Submit a Dreamina text-to-image task.

    Args:
        prompt: Generation prompt.
        ratio: Dreamina image ratio.
        resolution_type: Dreamina image resolution.
        model_version: Optional image model version.
        poll_seconds: Seconds to poll before returning.
        session: Dreamina session id.
        download: Whether to download parsed image URLs when polling completes.
        output_dir: Optional result directory.
        response_format: ``json`` or ``markdown``.

    Returns:
        Submit result, task id, and downloaded image paths when available.
    """
    args = ["text2image", "--prompt", prompt, "--ratio", ratio]
    _append_option(args, "--resolution_type", resolution_type)
    _append_option(args, "--model_version", model_version)
    _append_option(args, "--session", session)
    _append_option(args, "--poll", max(0, poll_seconds))
    return await _run_json(
        args,
        timeout=max(120, poll_seconds + 60),
        output_dir=output_dir or None,
        download=download,
        media_kind="image",
        prompt=prompt,
        response_format=response_format,
    )


@mcp.tool(
    name="dreamina_generate_marketing_image",
    annotations={
        "title": "Generate company marketing image",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def dreamina_generate_marketing_image(
    brief: str,
    aspect_ratio: str = "portrait",
    resolution_type: Literal["2k", "4k"] = "2k",
    model_version: str = "",
    poll_seconds: int = 600,
    download: bool = True,
    output_dir: str = "",
    response_format: ResponseFormat = "json",
) -> str:
    """Generate a reviewed Chinese marketing image via Dreamina.

    Args:
        brief: Business brief or reviewed creative direction.
        aspect_ratio: Business label such as ``portrait`` or Dreamina ratio.
        resolution_type: Dreamina resolution, usually ``2k`` for normal work.
        model_version: Optional image model version.
        poll_seconds: Seconds to wait for completion.
        download: Whether to download the generated image.
        output_dir: Optional result directory.
        response_format: ``json`` or ``markdown``.

    Returns:
        Structured Dreamina result with SOP media record.
    """
    prompt = build_structured_media_prompt(
        brief,
        media_kind="image",
        aspect_ratio=aspect_ratio,
        target_engine="dreamina",
    )
    ratio = normalize_ratio(aspect_ratio, default="9:16")
    return await dreamina_text2image(
        prompt=prompt,
        ratio=ratio,  # type: ignore[arg-type]
        resolution_type=resolution_type,
        model_version=model_version,
        poll_seconds=poll_seconds,
        download=download,
        output_dir=output_dir,
        response_format=response_format,
    )


@mcp.tool(
    name="dreamina_image2image",
    annotations={
        "title": "Generate or edit image from reference images",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def dreamina_image2image(
    images: list[str],
    prompt: str,
    ratio: ImageRatio = "1:1",
    resolution_type: Literal["2k", "4k"] = "2k",
    model_version: str = "",
    poll_seconds: int = 0,
    download: bool = True,
    output_dir: str = "",
    response_format: ResponseFormat = "json",
) -> str:
    """Submit a Dreamina image-to-image task.

    Args:
        images: One to ten local image paths.
        prompt: Edit or generation prompt.
        ratio: Dreamina output ratio.
        resolution_type: Dreamina output resolution.
        model_version: Optional image model version.
        poll_seconds: Seconds to poll before returning.
        download: Whether to download parsed image URLs.
        output_dir: Optional result directory.
        response_format: ``json`` or ``markdown``.

    Returns:
        Structured Dreamina result.
    """
    resolved_images = validate_existing_files(images, label="images")
    if len(resolved_images) > 10:
        raise ValueError("images supports at most 10 files.")
    args = ["image2image", "--prompt", prompt, "--images", ",".join(resolved_images)]
    _append_option(args, "--ratio", ratio)
    _append_option(args, "--resolution_type", resolution_type)
    _append_option(args, "--model_version", model_version)
    _append_option(args, "--poll", max(0, poll_seconds))
    return await _run_json(
        args,
        timeout=max(120, poll_seconds + 60),
        output_dir=output_dir or None,
        download=download,
        media_kind="image",
        prompt=prompt,
        response_format=response_format,
    )


@mcp.tool(
    name="dreamina_text2video",
    annotations={
        "title": "Generate video from text",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def dreamina_text2video(
    prompt: str,
    duration: int = 5,
    ratio: VideoRatio = "16:9",
    video_resolution: VideoResolution = "720p",
    model_version: str = "seedance2.0fast",
    poll_seconds: int = 0,
    download: bool = True,
    output_dir: str = "",
    response_format: ResponseFormat = "json",
) -> str:
    """Submit a Dreamina text-to-video task.

    Args:
        prompt: Video generation prompt.
        duration: Video duration in seconds.
        ratio: Dreamina video ratio.
        video_resolution: Dreamina video resolution.
        model_version: Optional video model version.
        poll_seconds: Seconds to poll before returning.
        download: Whether to download parsed video URLs.
        output_dir: Optional result directory.
        response_format: ``json`` or ``markdown``.

    Returns:
        Structured Dreamina result.
    """
    args = [
        "text2video",
        "--prompt",
        prompt,
        "--duration",
        str(max(4, min(duration, 15))),
        "--ratio",
        ratio,
    ]
    _append_option(args, "--video_resolution", video_resolution)
    _append_option(args, "--model_version", model_version)
    _append_option(args, "--poll", max(0, poll_seconds))
    return await _run_json(
        args,
        timeout=max(180, poll_seconds + 90),
        output_dir=output_dir or None,
        download=download,
        media_kind="video",
        prompt=prompt,
        response_format=response_format,
    )


@mcp.tool(
    name="dreamina_generate_short_video",
    annotations={
        "title": "Generate company short video",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def dreamina_generate_short_video(
    brief: str,
    duration: int = 5,
    aspect_ratio: str = "landscape",
    poll_seconds: int = 900,
    download: bool = True,
    output_dir: str = "",
    response_format: ResponseFormat = "json",
) -> str:
    """Generate a reviewed short video from a business brief.

    Args:
        brief: Business brief or reviewed storyboard direction.
        duration: Video duration in seconds.
        aspect_ratio: Business label such as ``landscape`` or Dreamina ratio.
        poll_seconds: Seconds to wait for completion.
        download: Whether to download the generated video.
        output_dir: Optional result directory.
        response_format: ``json`` or ``markdown``.

    Returns:
        Structured Dreamina result with SOP media record.
    """
    prompt = build_structured_media_prompt(brief, media_kind="video")
    ratio = normalize_ratio(aspect_ratio, default="16:9")
    return await dreamina_text2video(
        prompt=prompt,
        duration=duration,
        ratio=ratio,  # type: ignore[arg-type]
        poll_seconds=poll_seconds,
        download=download,
        output_dir=output_dir,
        response_format=response_format,
    )


@mcp.tool(
    name="dreamina_image2video",
    annotations={
        "title": "Animate one image into video",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def dreamina_image2video(
    image: str,
    prompt: str,
    duration: int = 5,
    video_resolution: VideoResolution = "720p",
    model_version: str = "",
    poll_seconds: int = 0,
    download: bool = True,
    output_dir: str = "",
    response_format: ResponseFormat = "json",
) -> str:
    """Animate a local image into a Dreamina video.

    Args:
        image: Local first-frame image path.
        prompt: Motion prompt.
        duration: Video duration in seconds.
        video_resolution: Dreamina video resolution.
        model_version: Optional model version.
        poll_seconds: Seconds to poll before returning.
        download: Whether to download parsed video URLs.
        output_dir: Optional result directory.
        response_format: ``json`` or ``markdown``.

    Returns:
        Structured Dreamina result.
    """
    image_path = validate_existing_file(image, label="image")
    args = ["image2video", "--image", image_path, "--prompt", prompt]
    _append_option(args, "--duration", max(3, min(duration, 15)))
    _append_option(args, "--video_resolution", video_resolution)
    _append_option(args, "--model_version", model_version)
    _append_option(args, "--poll", max(0, poll_seconds))
    return await _run_json(
        args,
        timeout=max(180, poll_seconds + 90),
        output_dir=output_dir or None,
        download=download,
        media_kind="image2video",
        prompt=prompt,
        response_format=response_format,
    )


@mcp.tool(
    name="dreamina_frames2video",
    annotations={
        "title": "Generate video from first and last frames",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def dreamina_frames2video(
    first: str,
    last: str,
    prompt: str,
    duration: int = 5,
    video_resolution: VideoResolution = "720p",
    model_version: str = "seedance2.0fast",
    poll_seconds: int = 0,
    download: bool = True,
    output_dir: str = "",
    response_format: ResponseFormat = "json",
) -> str:
    """Generate a video from first and last frame images.

    Args:
        first: Local first-frame image path.
        last: Local last-frame image path.
        prompt: Transition prompt.
        duration: Video duration in seconds.
        video_resolution: Dreamina video resolution.
        model_version: Optional model version.
        poll_seconds: Seconds to poll before returning.
        download: Whether to download parsed video URLs.
        output_dir: Optional result directory.
        response_format: ``json`` or ``markdown``.

    Returns:
        Structured Dreamina result.
    """
    args = [
        "frames2video",
        "--first",
        validate_existing_file(first, label="first"),
        "--last",
        validate_existing_file(last, label="last"),
        "--prompt",
        prompt,
        "--duration",
        str(max(3, min(duration, 15))),
    ]
    _append_option(args, "--video_resolution", video_resolution)
    _append_option(args, "--model_version", model_version)
    _append_option(args, "--poll", max(0, poll_seconds))
    return await _run_json(
        args,
        timeout=max(180, poll_seconds + 90),
        output_dir=output_dir or None,
        download=download,
        media_kind="video",
        prompt=prompt,
        response_format=response_format,
    )


@mcp.tool(
    name="dreamina_multiframe2video",
    annotations={
        "title": "Generate coherent video from multiple frames",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def dreamina_multiframe2video(
    images: list[str],
    prompt: str = "",
    transition_prompts: list[str] | None = None,
    transition_durations: list[float] | None = None,
    poll_seconds: int = 0,
    download: bool = True,
    output_dir: str = "",
    response_format: ResponseFormat = "json",
) -> str:
    """Generate a video story from multiple local frame images.

    Args:
        images: Two to twenty local image paths.
        prompt: Shorthand prompt for exactly two images.
        transition_prompts: Prompts for each image transition.
        transition_durations: Durations for each transition segment.
        poll_seconds: Seconds to poll before returning.
        download: Whether to download parsed video URLs.
        output_dir: Optional result directory.
        response_format: ``json`` or ``markdown``.

    Returns:
        Structured Dreamina result.
    """
    resolved_images = validate_existing_files(images, label="images")
    if not 2 <= len(resolved_images) <= 20:
        raise ValueError("images must contain 2 to 20 files.")
    args = ["multiframe2video", "--images", ",".join(resolved_images)]
    if prompt:
        args.extend(["--prompt", prompt])
    for item in transition_prompts or []:
        args.extend(["--transition-prompt", item])
    for item in transition_durations or []:
        args.extend(["--transition-duration", str(item)])
    _append_option(args, "--poll", max(0, poll_seconds))
    return await _run_json(
        args,
        timeout=max(180, poll_seconds + 90),
        output_dir=output_dir or None,
        download=download,
        media_kind="video",
        prompt=prompt or "multi-frame video",
        response_format=response_format,
    )


@mcp.tool(
    name="dreamina_multimodal2video",
    annotations={
        "title": "Generate video from multimodal references",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def dreamina_multimodal2video(
    prompt: str = "",
    images: list[str] | None = None,
    videos: list[str] | None = None,
    audio: list[str] | None = None,
    duration: int = 5,
    ratio: VideoRatio = "16:9",
    video_resolution: VideoResolution = "720p",
    model_version: str = "seedance2.0fast",
    poll_seconds: int = 0,
    download: bool = True,
    output_dir: str = "",
    response_format: ResponseFormat = "json",
) -> str:
    """Generate a video using Dreamina all-around references.

    Args:
        prompt: Optional edit or generation prompt.
        images: Local image reference paths.
        videos: Local video reference paths.
        audio: Local audio reference paths.
        duration: Video duration in seconds.
        ratio: Dreamina video ratio.
        video_resolution: Dreamina video resolution.
        model_version: Seedance 2.0 family model version.
        poll_seconds: Seconds to poll before returning.
        download: Whether to download parsed video URLs.
        output_dir: Optional result directory.
        response_format: ``json`` or ``markdown``.

    Returns:
        Structured Dreamina result.
    """
    resolved_images = [
        validate_existing_file(path, label="images") for path in images or []
    ]
    resolved_videos = [
        validate_existing_file(path, label="videos") for path in videos or []
    ]
    resolved_audio = [
        validate_existing_file(path, label="audio") for path in audio or []
    ]
    if not resolved_images and not resolved_videos:
        raise ValueError("At least one image or video reference is required.")
    args = ["multimodal2video"]
    for path in resolved_images:
        args.extend(["--image", path])
    for path in resolved_videos:
        args.extend(["--video", path])
    for path in resolved_audio:
        args.extend(["--audio", path])
    _append_option(args, "--prompt", prompt)
    _append_option(args, "--duration", max(4, min(duration, 15)))
    _append_option(args, "--ratio", ratio)
    _append_option(args, "--video_resolution", video_resolution)
    _append_option(args, "--model_version", model_version)
    _append_option(args, "--poll", max(0, poll_seconds))
    return await _run_json(
        args,
        timeout=max(180, poll_seconds + 90),
        output_dir=output_dir or None,
        download=download,
        media_kind="video",
        prompt=prompt or "multimodal reference video",
        response_format=response_format,
    )


@mcp.tool(
    name="dreamina_image_upscale",
    annotations={
        "title": "Upscale one image",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def dreamina_image_upscale(
    image: str,
    resolution_type: UpscaleResolution = "2k",
    poll_seconds: int = 0,
    download: bool = True,
    output_dir: str = "",
    response_format: ResponseFormat = "json",
) -> str:
    """Upscale one local image with Dreamina.

    Args:
        image: Local image path.
        resolution_type: Target resolution. ``4k`` and ``8k`` may require VIP.
        poll_seconds: Seconds to poll before returning.
        download: Whether to download parsed image URLs.
        output_dir: Optional result directory.
        response_format: ``json`` or ``markdown``.

    Returns:
        Structured Dreamina result.
    """
    args = [
        "image_upscale",
        "--image",
        validate_existing_file(image, label="image"),
        "--resolution_type",
        resolution_type,
    ]
    _append_option(args, "--poll", max(0, poll_seconds))
    return await _run_json(
        args,
        timeout=max(180, poll_seconds + 90),
        output_dir=output_dir or None,
        download=download,
        media_kind="image",
        prompt=f"upscale image to {resolution_type}",
        response_format=response_format,
    )


def main() -> None:
    """Run the Dreamina MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
