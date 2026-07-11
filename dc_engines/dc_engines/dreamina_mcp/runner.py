"""Shared Dreamina CLI runner used by the MCP server."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from dc_engines.dreamina_cli import (
    build_dreamina_subprocess_env,
    dreamina_command_not_found_message,
    resolve_dreamina_executable,
)
from dc_engines.media_sop import build_media_generation_record

MEDIA_URL_RE = re.compile(
    r"https?://[^\s<>\"']+\.(?:jpg|jpeg|png|webp|mp4|mov|m4v|mp3|wav|m4a)"
    r"(?:\?[^\s<>\"']*)?",
    re.IGNORECASE,
)
LOCAL_MEDIA_RE = re.compile(
    r"(?P<path>(?:/|~\/)[^\s<>\"']+\.(?:jpg|jpeg|png|webp|mp4|mov|m4v|mp3|wav|m4a))",
    re.IGNORECASE,
)

ASPECT_RATIO_MAP = {
    "landscape": "16:9",
    "wide": "16:9",
    "square": "1:1",
    "portrait": "9:16",
    "vertical": "9:16",
    "wechat_cover": "16:9",
    "rednote_cover": "3:4",
}


@dataclass(frozen=True, slots=True)
class DreaminaRunResult:
    """Structured result returned by a Dreamina CLI invocation.

    Args:
        ok: Whether the CLI command returned success and no failed generation
            status was detected.
        command: Redacted command array executed by the runner.
        returncode: CLI process return code.
        stdout: Captured stdout.
        stderr: Captured stderr.
        parsed_json: JSON objects parsed from stdout/stderr.
        submit_id: Dreamina submit identifier when present.
        gen_status: Dreamina generation status when present.
        fail_reason: Dreamina failure reason when present.
        media_urls: Result media URLs parsed from output.
        downloaded_files: Local files downloaded or reported by the CLI.
        output_dir: Directory used for result downloads and audit logs.
        started_at: UTC ISO timestamp for command start.
        finished_at: UTC ISO timestamp for command finish.
        task_state: Standardized async task state.
        result_ready: Whether result media is ready for downstream use.
        next_action: Suggested next MCP action for the caller.
        error_hint: Actionable failure hint when the command failed.
        record: Media generation record dictionary when this command represents
            a media workflow.
    """

    ok: bool
    command: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    parsed_json: list[Any]
    submit_id: str
    gen_status: str
    fail_reason: str
    media_urls: list[str]
    downloaded_files: list[str]
    output_dir: str
    started_at: str
    finished_at: str
    task_state: str = "unknown"
    result_ready: bool = False
    next_action: str = ""
    error_hint: str = ""
    record: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the result as a JSON-serializable dictionary.

        Returns:
            JSON-serializable command result.
        """
        return asdict(self)


def output_dir_from_env(output_dir: str | None = None) -> Path:
    """Resolve and create the Dreamina output directory.

    Args:
        output_dir: Optional caller-provided output directory.

    Returns:
        Existing directory path for media outputs and audit logs.
    """
    raw = (
        output_dir
        or os.environ.get("DREAMINA_OUTPUT_DIR")
        or str(Path.home() / "DC-Agent" / "hermes-config" / "cache" / "dreamina")
    )
    path = Path(raw).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_ratio(aspect_ratio: str | None, default: str = "1:1") -> str:
    """Normalize business aspect labels to Dreamina ratio values.

    Args:
        aspect_ratio: A Dreamina ratio such as ``9:16`` or a business label
            such as ``portrait``.
        default: Ratio to return when no aspect ratio is provided.

    Returns:
        Dreamina-compatible ratio value.
    """
    if not aspect_ratio:
        return default
    value = aspect_ratio.strip()
    return ASPECT_RATIO_MAP.get(value, value)


def validate_existing_file(path: str, *, label: str = "file") -> str:
    """Resolve and validate a local input file path.

    Args:
        path: User-provided file path.
        label: Human-readable field name used in error messages.

    Returns:
        Absolute file path.

    Raises:
        ValueError: The path is empty or does not point to a file.
    """
    if not path or not path.strip():
        raise ValueError(f"{label} is required.")
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"{label} does not exist or is not a file: {resolved}")
    return str(resolved)


def validate_existing_files(paths: list[str], *, label: str) -> list[str]:
    """Resolve and validate a list of local input files.

    Args:
        paths: User-provided file paths.
        label: Human-readable field name used in error messages.

    Returns:
        Absolute file paths.

    Raises:
        ValueError: No paths are provided or any path is invalid.
    """
    if not paths:
        raise ValueError(f"{label} must contain at least one file.")
    return [validate_existing_file(path, label=label) for path in paths]


def _build_cli_env() -> dict[str, str]:
    """Build the subprocess environment for Dreamina CLI.

    Returns:
        Environment variables with Dreamina-specific path/proxy compatibility.
    """
    base_env = dict(os.environ)
    if base_env.get("DREAMINA_BIN") and not base_env.get("DREAMINA_CLI_PATH"):
        base_env["DREAMINA_CLI_PATH"] = base_env["DREAMINA_BIN"]
    return build_dreamina_subprocess_env(base_env)


def _extract_json_values(text: str) -> list[Any]:
    """Extract JSON values embedded in mixed CLI text.

    Args:
        text: Mixed stdout/stderr content.

    Returns:
        Parsed JSON values found in the text.
    """
    values: list[Any] = []
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        next_positions = [
            pos for pos in (text.find("{", index), text.find("[", index)) if pos >= 0
        ]
        if not next_positions:
            break
        start = min(next_positions)
        try:
            value, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        values.append(value)
        index = start + end
    return values


def _find_value(values: list[Any], key: str) -> Any:
    """Find the first value for a key inside parsed JSON structures.

    Args:
        values: JSON values to search.
        key: Dictionary key to locate.

    Returns:
        Matching value, or an empty string when not found.
    """
    stack = list(values)
    while stack:
        value = stack.pop(0)
        if isinstance(value, dict):
            if key in value:
                return value[key]
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return ""


def _parse_submit_id(text: str, values: list[Any]) -> str:
    """Extract a Dreamina submit_id from CLI output.

    Args:
        text: Mixed stdout/stderr content.
        values: Parsed JSON values from the same output.

    Returns:
        Submit id, or an empty string when not present.
    """
    from_json = _find_value(values, "submit_id")
    if from_json:
        return str(from_json)
    match = re.search(r"submit_id[\"'\s:=]+([A-Za-z0-9_-]+)", text)
    return match.group(1) if match else ""


def _is_concurrency_limit(text: str) -> bool:
    """Return whether Dreamina reported an account-level concurrency limit.

    Args:
        text: CLI stdout, stderr, or parsed failure text.

    Returns:
        True when the text contains Dreamina's concurrency-limit marker.
    """
    return "ExceedConcurrencyLimit" in str(text or "")


def _concurrency_limit_hint() -> str:
    """Return the user-facing Dreamina concurrency-limit hint.

    Returns:
        A short Chinese diagnostic for video/image generation callers.
    """
    return "即梦生成并发超限，请等待上一条任务结束或平台释放并发后再重试。"


def _is_history_query_failure(text: str) -> bool:
    """Return whether Dreamina failed while querying task history.

    Args:
        text: CLI stdout, stderr, or parsed failure text.

    Returns:
        True when the text contains Dreamina's history-query failure marker.
    """
    normalized = str(text or "")
    return "get_history_by_ids" in normalized or "ret=1015" in normalized


def _history_query_failure_hint(text: str) -> str:
    """Return a user-facing hint for Dreamina history-query failures.

    Args:
        text: CLI stdout, stderr, or parsed failure text.

    Returns:
        A short Chinese diagnostic with the Dreamina return code.
    """
    match = re.search(r"ret=(\d+)", str(text or ""))
    code = f"ret={match.group(1)}" if match else "错误码未知"
    return (
        f"即梦任务已进入结果查询阶段，但历史查询接口返回异常（{code}）。"
        "请稍后重试；如果连续出现，需要检查 Dreamina CLI/MCP 登录态、账号额度或服务端状态。"
    )


def _standard_task_state(
    *,
    returncode: int | None,
    submit_id: str,
    gen_status: str,
    fail_reason: str,
    media_urls: list[str],
    downloaded_files: list[str],
    error_hint: str,
) -> tuple[str, bool, str]:
    """Normalize Dreamina async task status for MCP callers.

    Args:
        returncode: CLI process return code.
        submit_id: Dreamina submit id when present.
        gen_status: Raw Dreamina generation status.
        fail_reason: Raw Dreamina failure reason.
        media_urls: Parsed output media URLs.
        downloaded_files: Parsed or downloaded local files.
        error_hint: Current failure hint.

    Returns:
        Tuple of task state, result readiness, and suggested next action.
    """
    status = str(gen_status or "").strip().lower()
    if returncode is None:
        return "failed", False, "检查 Dreamina CLI 是否可用后重试。"
    if returncode != 0 or status == "fail" or fail_reason or error_hint:
        return "failed", False, "根据 error_hint 处理后重新提交任务。"
    if media_urls or downloaded_files or status in {"success", "succeeded", "done"}:
        return "succeeded", True, "结果已就绪，可使用 media_urls 或 downloaded_files。"
    if submit_id:
        return (
            "submitted",
            False,
            f"稍后调用 dreamina_query_result 查询 submit_id={submit_id}。",
        )
    return "unknown", False, "未解析到 submit_id 或结果，请查看 stdout/stderr。"


def _media_urls(text: str, values: list[Any]) -> list[str]:
    """Extract result media URLs from text and parsed JSON.

    Args:
        text: Mixed stdout/stderr content.
        values: Parsed JSON values from the same output.

    Returns:
        De-duplicated media URLs.
    """
    urls = MEDIA_URL_RE.findall(text)
    stack = list(values)
    while stack:
        value = stack.pop(0)
        if isinstance(value, str) and MEDIA_URL_RE.match(value):
            urls.append(value)
        elif isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return list(dict.fromkeys(urls))


def _local_media_files(text: str) -> list[str]:
    """Extract local media file paths reported by the CLI.

    Args:
        text: Mixed stdout/stderr content.

    Returns:
        Existing local media file paths.
    """
    files: list[str] = []
    for match in LOCAL_MEDIA_RE.finditer(text):
        path = Path(match.group("path")).expanduser()
        if path.is_file():
            files.append(str(path.resolve()))
    return list(dict.fromkeys(files))


def _download_media_urls(
    urls: list[str],
    output_dir: Path,
    *,
    prefix: str,
) -> list[str]:
    """Download media URLs into the output directory.

    Args:
        urls: Remote media URLs.
        output_dir: Directory where files should be saved.
        prefix: File name prefix.

    Returns:
        Paths to successfully downloaded files.
    """
    downloaded: list[str] = []
    for index, url in enumerate(urls, start=1):
        parsed = urllib.parse.urlparse(url)
        suffix = Path(parsed.path).suffix or ".bin"
        filename = f"{prefix}_{index:02d}{suffix}"
        target = output_dir / filename
        urllib.request.urlretrieve(url, target)
        downloaded.append(str(target))
    return downloaded


def _append_audit_log(output_dir: Path, payload: dict[str, Any]) -> None:
    """Append one Dreamina MCP audit record.

    Args:
        output_dir: Directory containing the audit log.
        payload: JSON-serializable audit payload.
    """
    audit_path = output_dir / "dreamina_mcp_tasks.jsonl"
    with audit_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


async def run_dreamina(
    args: list[str],
    *,
    timeout: int = 900,
    output_dir: str | None = None,
    download: bool = False,
    media_kind: Literal["image", "video", "image2video"] | None = None,
    prompt: str = "",
    engine: str = "Dreamina 即梦",
) -> DreaminaRunResult:
    """Run the Dreamina CLI and return a structured result.

    Args:
        args: Dreamina CLI arguments after the executable name.
        timeout: Subprocess timeout in seconds.
        output_dir: Optional directory for downloads and audit logs.
        download: Whether parsed media URLs should be downloaded locally.
        media_kind: Optional media kind for SOP record creation.
        prompt: Prompt used for media generation records.
        engine: Human-readable engine name for media records.

    Returns:
        Structured Dreamina command result.
    """
    target_dir = output_dir_from_env(output_dir)
    env = _build_cli_env()
    dreamina_bin = resolve_dreamina_executable(env)
    started_at = datetime.now(UTC).isoformat()
    if dreamina_bin is None:
        finished_at = datetime.now(UTC).isoformat()
        result = DreaminaRunResult(
            ok=False,
            command=["dreamina", *args],
            returncode=None,
            stdout="",
            stderr="",
            parsed_json=[],
            submit_id="",
            gen_status="",
            fail_reason="",
            media_urls=[],
            downloaded_files=[],
            output_dir=str(target_dir),
            started_at=started_at,
            finished_at=finished_at,
            task_state="failed",
            result_ready=False,
            next_action="检查 Dreamina CLI 是否可用后重试。",
            error_hint=dreamina_command_not_found_message(),
        )
        _append_audit_log(target_dir, result.to_dict())
        return result

    command = [dreamina_bin, *args]
    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(target_dir),
            check=False,
        )
    except subprocess.TimeoutExpired:
        finished_at = datetime.now(UTC).isoformat()
        result = DreaminaRunResult(
            ok=False,
            command=command,
            returncode=None,
            stdout="",
            stderr="",
            parsed_json=[],
            submit_id="",
            gen_status="",
            fail_reason="",
            media_urls=[],
            downloaded_files=[],
            output_dir=str(target_dir),
            started_at=started_at,
            finished_at=finished_at,
            task_state="failed",
            result_ready=False,
            next_action="缩短 poll_seconds 或稍后用 dreamina_query_result 查询任务。",
            error_hint=f"Dreamina CLI timed out after {timeout} seconds.",
        )
        _append_audit_log(target_dir, result.to_dict())
        return result

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    combined = f"{stdout}\n{stderr}".strip()
    parsed_json = _extract_json_values(combined)
    submit_id = _parse_submit_id(combined, parsed_json)
    gen_status = str(_find_value(parsed_json, "gen_status") or "")
    fail_reason = str(_find_value(parsed_json, "fail_reason") or "")
    urls = _media_urls(combined, parsed_json)
    downloaded_files = _local_media_files(combined)
    ok = completed.returncode == 0 and gen_status != "fail"
    error_hint = ""
    if completed.returncode != 0:
        error_hint = (
            combined[:1000]
            or "Dreamina CLI failed. Check login status, credits, network, and parameters."
        )
    elif gen_status == "fail":
        error_hint = fail_reason or "Dreamina generation task failed."
    if _is_concurrency_limit(combined) or _is_concurrency_limit(fail_reason):
        error_hint = _concurrency_limit_hint()
    elif _is_history_query_failure(combined) or _is_history_query_failure(fail_reason):
        error_hint = _history_query_failure_hint(combined or fail_reason)
    task_state, result_ready, next_action = _standard_task_state(
        returncode=completed.returncode,
        submit_id=submit_id,
        gen_status=gen_status,
        fail_reason=fail_reason,
        media_urls=urls,
        downloaded_files=downloaded_files,
        error_hint=error_hint,
    )

    if download and urls:
        try:
            prefix = (
                datetime.now().strftime("dreamina_%Y%m%d_%H%M%S")
                + f"_{uuid4().hex[:8]}"
            )
            downloaded_files.extend(
                _download_media_urls(urls, target_dir, prefix=prefix)
            )
            task_state, result_ready, next_action = _standard_task_state(
                returncode=completed.returncode,
                submit_id=submit_id,
                gen_status=gen_status,
                fail_reason=fail_reason,
                media_urls=urls,
                downloaded_files=downloaded_files,
                error_hint=error_hint,
            )
        except Exception as exc:
            ok = False
            error_hint = f"Dreamina result download failed: {exc}"
            task_state, result_ready, next_action = _standard_task_state(
                returncode=completed.returncode,
                submit_id=submit_id,
                gen_status=gen_status,
                fail_reason=fail_reason,
                media_urls=urls,
                downloaded_files=downloaded_files,
                error_hint=error_hint,
            )

    record: dict[str, Any] | None = None
    if media_kind and (result_ready or task_state == "failed"):
        output_path = downloaded_files[0] if downloaded_files else ""
        output_url = urls[0] if urls else ""
        media_record = build_media_generation_record(
            media_kind=media_kind,
            prompt=prompt,
            engine=engine,
            status="succeeded" if result_ready else "failed",
            output_path=output_path,
            output_url=output_url,
            error_hint=error_hint,
        )
        record = media_record.to_dict()

    finished_at = datetime.now(UTC).isoformat()
    result = DreaminaRunResult(
        ok=ok,
        command=command,
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        parsed_json=parsed_json,
        submit_id=submit_id,
        gen_status=gen_status,
        fail_reason=fail_reason,
        media_urls=urls,
        downloaded_files=list(dict.fromkeys(downloaded_files)),
        output_dir=str(target_dir),
        started_at=started_at,
        finished_at=finished_at,
        task_state=task_state,
        result_ready=result_ready,
        next_action=next_action,
        error_hint=error_hint,
        record=record,
    )
    _append_audit_log(target_dir, result.to_dict())
    return result


def result_json(result: DreaminaRunResult) -> str:
    """Serialize a Dreamina result for MCP clients.

    Args:
        result: Structured Dreamina command result.

    Returns:
        Pretty JSON string.
    """
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
