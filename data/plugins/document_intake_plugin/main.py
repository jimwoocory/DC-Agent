"""Automatic document intake for message attachments.

This plugin turns uploaded file components into a no-touch background workflow:
copy to the NAS inbox, parse a bounded excerpt into the current LLM context, and
schedule a full knowledge-base import with stable source_path metadata.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import File
from astrbot.api.star import Context, Star, register
from astrbot.core.knowledge_base.parsers.util import select_parser

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INBOX_DIR = PROJECT_ROOT / "nas" / "knowledge" / "inbox" / "download"
DEFAULT_KB_NAMES = ("nas_knowledge", "营销素材", "中台运营")
SUPPORTED_SUFFIXES = {
    ".docx",
    ".epub",
    ".md",
    ".markdown",
    ".pdf",
    ".pptx",
    ".txt",
    ".xls",
    ".xlsx",
}
DEFAULT_MAX_FILE_MB = 80
DEFAULT_CONTEXT_CHARS = 12000
_KB_IMPORT_INTENT_RE = re.compile(
    r"((入库|归档|保存|存入|同步|导入).{0,10}(知识库|资料库|素材库))|"
    r"((知识库|资料库|素材库).{0,10}(入库|归档|保存|存入|同步|导入))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IntakeResult:
    original_name: str
    stored_path: Path
    sha256: str
    size_bytes: int
    parsed_text: str
    status: str
    error: str = ""


@dataclass(frozen=True)
class DocumentIntakeCardHandle:
    streamer: Any
    message_id: str
    platform_id: str


def _replace_event_text(event: AstrMessageEvent, text: str) -> None:
    event.message_str = text
    try:
        event.message_obj.message_str = text
    except Exception:  # noqa: BLE001
        pass


def _requests_kb_import(text: str) -> bool:
    """Return whether the user explicitly asks to persist files to a KB."""
    return bool(_KB_IMPORT_INTENT_RE.search(text or ""))


def _safe_file_name(file_name: str) -> str:
    raw = Path(file_name or "attachment").name
    stem = Path(raw).stem or "attachment"
    suffix = Path(raw).suffix.lower()
    stem = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", stem).strip("._")
    if not stem:
        stem = "attachment"
    return f"{stem[:96]}{suffix}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_target_path(inbox_dir: Path, file_name: str) -> Path:
    safe_name = _safe_file_name(file_name)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = inbox_dir / f"{timestamp}-{safe_name}"
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(1, 1000):
        next_candidate = inbox_dir / f"{stem}-{index}{suffix}"
        if not next_candidate.exists():
            return next_candidate
    raise RuntimeError(f"Unable to allocate import path for {safe_name}")


def _build_context_block(results: list[IntakeResult], max_chars: int) -> str:
    blocks: list[str] = []
    budget = max(500, max_chars)
    for item in results:
        header = (
            f"文件：{item.original_name}\n"
            f"NAS路径：{item.stored_path}\n"
            f"sha256：{item.sha256}\n"
            f"状态：{item.status}"
        )
        if item.error:
            header += f"\n错误：{item.error}"
        text = item.parsed_text.strip()
        remaining = max(0, budget - len("\n\n".join(blocks)) - len(header) - 64)
        if text and remaining > 0:
            excerpt = text[:remaining]
            if len(text) > remaining:
                excerpt += "\n...[内容已截断，完整文件已后台入库]"
            blocks.append(f"{header}\n\n解析内容：\n{excerpt}")
        else:
            blocks.append(header)
    body = "\n\n---\n\n".join(blocks)
    return f"<dc_document_intake>\n{body}\n</dc_document_intake>"


def _results_as_card_files(results: list[IntakeResult]) -> list[dict[str, Any]]:
    return [
        {
            "name": item.original_name,
            "status": item.status,
            "size_bytes": item.size_bytes,
            "error": item.error,
            "stored_path": str(item.stored_path),
        }
        for item in results
    ]


def _initial_card_files(files: list[File]) -> list[dict[str, Any]]:
    return [
        {
            "name": component.name or "attachment",
            "status": "等待下载",
            "size_bytes": 0,
        }
        for component in files
    ]


def _status_counts(results: list[IntakeResult]) -> dict[str, int]:
    counts = {
        "failed": 0,
        "imported": 0,
        "unsupported": 0,
    }
    for item in results:
        if item.status == "unsupported":
            counts["unsupported"] += 1
        elif item.status in {"failed", "too_large"}:
            counts["failed"] += 1
    return counts


async def _start_document_intake_card(
    context: Context,
    event: AstrMessageEvent,
    *,
    files: list[File],
    inbox_path: Path,
) -> DocumentIntakeCardHandle | None:
    try:
        from dc_engines.card_runtime import send_card_via_runtime
        from dc_engines.feishu_card_streamer import (
            build_document_intake_card,
            ensure_streamers_on_context,
            extract_chat_info_from_event,
        )
    except Exception:  # noqa: BLE001
        return None

    platform_id = ""
    get_platform_id = getattr(event, "get_platform_id", None)
    if callable(get_platform_id):
        platform_id = get_platform_id() or ""
    streamer = ensure_streamers_on_context(context).get(platform_id)
    if streamer is None:
        return None

    chat_id, receive_id_type = extract_chat_info_from_event(event)
    if not chat_id:
        return None

    stream = await send_card_via_runtime(
        streamer,
        card_type="document_intake",
        chat_id=chat_id,
        receive_id_type=receive_id_type,
        card=build_document_intake_card(
            status="接收中",
            files=_initial_card_files(files),
            inbox_path=str(inbox_path),
            summary_note="正在下载、复制并解析上传文件。",
        ),
        platform_id=platform_id,
        event="start",
        detail=f"document intake started files={len(files)}",
    )
    if stream is None:
        return None
    return DocumentIntakeCardHandle(
        streamer=streamer,
        message_id=stream.message_id,
        platform_id=platform_id,
    )


async def _finalize_document_intake_card(
    handle: DocumentIntakeCardHandle | None,
    *,
    results: list[IntakeResult],
    kb_summary: dict[str, Any] | None = None,
    inbox_path: Path,
    auto_import: bool,
) -> bool:
    if handle is None:
        return False
    try:
        from dc_engines.card_runtime import finalize_card_via_runtime
        from dc_engines.feishu_card_streamer import build_document_intake_card
    except Exception:  # noqa: BLE001
        return False

    counts = _status_counts(results)
    imported_count = int((kb_summary or {}).get("imported_count") or 0)
    failed_count = counts["failed"] + int((kb_summary or {}).get("failed_count") or 0)
    unsupported_count = counts["unsupported"]
    kb_name = str((kb_summary or {}).get("kb_name") or "")

    if imported_count and failed_count == 0 and unsupported_count == 0:
        status = "已入库"
    elif imported_count or any(
        result.status in {"parsed", "copied"} for result in results
    ):
        status = "部分完成" if failed_count or unsupported_count else "已解析"
    else:
        status = "入库失败" if auto_import else "失败"

    if auto_import:
        if kb_summary is None:
            note = "文件已复制并解析，后台知识库入库已启动。"
            status = "入库中"
        elif imported_count:
            note = "已复制到 NAS，并完成后台知识库导入。"
        else:
            note = str(
                (kb_summary or {}).get("error")
                or "文件已复制到 NAS，知识库入库未完成。"
            )
    else:
        note = "已复制到 NAS，并把解析摘要注入当前对话。"

    return await finalize_card_via_runtime(
        handle.streamer,
        card_type="document_intake",
        message_id=handle.message_id,
        card=build_document_intake_card(
            status=status,
            files=_results_as_card_files(results),
            kb_name=kb_name,
            inbox_path=str(inbox_path),
            summary_note=note,
            imported_count=imported_count,
            failed_count=failed_count,
            unsupported_count=unsupported_count,
        ),
        platform_id=handle.platform_id,
        detail=f"document intake finalized files={len(results)} status={status}",
    )


def _message_files(event: AstrMessageEvent) -> list[File]:
    try:
        components = list(event.message_obj.message)
    except Exception:  # noqa: BLE001
        return []
    return [component for component in components if isinstance(component, File)]


async def _copy_component_to_inbox(
    component: File,
    *,
    inbox_dir: Path,
    supported_suffixes: set[str],
    max_file_mb: int,
) -> IntakeResult:
    original_name = component.name or "attachment"
    suffix = Path(original_name).suffix.lower()
    if suffix not in supported_suffixes:
        return IntakeResult(
            original_name=original_name,
            stored_path=inbox_dir / _safe_file_name(original_name),
            sha256="",
            size_bytes=0,
            parsed_text="",
            status="unsupported",
            error=f"Unsupported file type: {suffix or '(none)'}",
        )

    source = await component.get_file()
    if not source:
        return IntakeResult(
            original_name=original_name,
            stored_path=inbox_dir / _safe_file_name(original_name),
            sha256="",
            size_bytes=0,
            parsed_text="",
            status="failed",
            error="Attachment did not provide a local file path.",
        )

    source_path = Path(source).expanduser().resolve()
    if not source_path.exists() or not source_path.is_file():
        return IntakeResult(
            original_name=original_name,
            stored_path=inbox_dir / _safe_file_name(original_name),
            sha256="",
            size_bytes=0,
            parsed_text="",
            status="failed",
            error=f"Attachment file does not exist: {source_path}",
        )

    size_bytes = source_path.stat().st_size
    max_bytes = max_file_mb * 1024 * 1024
    if size_bytes > max_bytes:
        return IntakeResult(
            original_name=original_name,
            stored_path=inbox_dir / _safe_file_name(original_name),
            sha256="",
            size_bytes=size_bytes,
            parsed_text="",
            status="too_large",
            error=f"File exceeds {max_file_mb} MB limit.",
        )

    inbox_dir.mkdir(parents=True, exist_ok=True)
    stored_path = _unique_target_path(inbox_dir, original_name)
    await asyncio.to_thread(shutil.copy2, source_path, stored_path)
    sha256 = await asyncio.to_thread(_sha256_file, stored_path)
    parsed_text = ""
    status = "copied"
    error = ""
    try:
        file_content = await asyncio.to_thread(stored_path.read_bytes)
        parser = await select_parser(stored_path.suffix.lower())
        parsed = await parser.parse(file_content, stored_path.name)
        parsed_text = parsed.text.strip()
        status = "parsed" if parsed_text else "parsed_empty"
    except Exception as exc:  # noqa: BLE001
        status = "parse_failed"
        error = str(exc)
        logger.warning(
            "[document_intake] parse failed file=%s: %s",
            stored_path,
            exc,
        )

    return IntakeResult(
        original_name=original_name,
        stored_path=stored_path,
        sha256=sha256,
        size_bytes=size_bytes,
        parsed_text=parsed_text,
        status=status,
        error=error,
    )


@register(
    "document_intake_plugin",
    "dc_agent",
    "Automatic NAS + KB intake for uploaded documents",
    "0.1.0",
)
class DocumentIntakePlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context, config)
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.inbox_dir = Path(cfg.get("inbox_dir") or DEFAULT_INBOX_DIR).expanduser()
        suffixes = cfg.get("supported_suffixes") or sorted(SUPPORTED_SUFFIXES)
        self.supported_suffixes = {str(suffix).lower() for suffix in suffixes}
        self.max_file_mb = int(cfg.get("max_file_mb", DEFAULT_MAX_FILE_MB))
        self.context_chars = int(cfg.get("context_chars", DEFAULT_CONTEXT_CHARS))
        kb_names = cfg.get("kb_names") or DEFAULT_KB_NAMES
        self.kb_names = tuple(str(name) for name in kb_names if str(name).strip())
        self.auto_import = bool(cfg.get("auto_import", True))
        self.inject_context = bool(cfg.get("inject_context", True))

    async def initialize(self) -> None:
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[document_intake] NAS inbox ready: %s", self.inbox_dir)

    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE,
        priority=95,
    )
    async def on_message(self, event: AstrMessageEvent):
        if not self.enabled:
            return
        files = _message_files(event)
        if not files:
            return

        card_handle = await _start_document_intake_card(
            self.context,
            event,
            files=files,
            inbox_path=self.inbox_dir,
        )

        results = [
            await _copy_component_to_inbox(
                component,
                inbox_dir=self.inbox_dir,
                supported_suffixes=self.supported_suffixes,
                max_file_mb=self.max_file_mb,
            )
            for component in files
        ]
        ready_results = [result for result in results if result.status != "unsupported"]
        should_auto_import = self.auto_import and _requests_kb_import(
            event.message_str or ""
        )
        if ready_results and should_auto_import:
            try:
                asyncio.create_task(
                    self._upload_results_to_kb_and_finalize_card(
                        ready_results,
                        all_results=results,
                        card_handle=card_handle,
                    )
                )
            except RuntimeError as exc:
                logger.warning("[document_intake] cannot schedule kb import: %s", exc)
                await _finalize_document_intake_card(
                    card_handle,
                    results=results,
                    kb_summary={"error": str(exc)},
                    inbox_path=self.inbox_dir,
                    auto_import=should_auto_import,
                )
        else:
            await _finalize_document_intake_card(
                card_handle,
                results=results,
                kb_summary=None,
                inbox_path=self.inbox_dir,
                auto_import=False,
            )

        if self.inject_context:
            block = _build_context_block(results, self.context_chars)
            original = (event.message_str or "").strip()
            merged = f"{original}\n\n{block}" if original else block
            _replace_event_text(event, merged)

        event.set_extra(
            "dc_document_intake",
            [
                {
                    "original_name": item.original_name,
                    "stored_path": str(item.stored_path),
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                    "status": item.status,
                    "error": item.error,
                }
                for item in results
            ],
        )

    async def _upload_results_to_kb_and_finalize_card(
        self,
        results: list[IntakeResult],
        *,
        all_results: list[IntakeResult],
        card_handle: DocumentIntakeCardHandle | None,
    ) -> None:
        try:
            kb_summary = await self._upload_results_to_kb(results)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[document_intake] kb import task failed: %s", exc)
            kb_summary = {
                "kb_name": "",
                "imported_count": 0,
                "failed_count": len(results),
                "error": str(exc),
            }
        await _finalize_document_intake_card(
            card_handle,
            results=all_results,
            kb_summary=kb_summary,
            inbox_path=self.inbox_dir,
            auto_import=self.auto_import,
        )

    async def _upload_results_to_kb(
        self, results: list[IntakeResult]
    ) -> dict[str, Any]:
        kb_manager = getattr(self.context, "kb_manager", None)
        if kb_manager is None:
            return {
                "kb_name": "",
                "imported_count": 0,
                "failed_count": 0,
                "error": "Knowledge base manager is not available.",
            }
        helper = None
        kb_name = ""
        for name in self.kb_names:
            try:
                helper = await kb_manager.get_kb_by_name(name)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[document_intake] kb lookup failed name=%s: %s", name, exc
                )
                helper = None
            if helper is not None:
                kb_name = name
                break
        if helper is None:
            logger.warning("[document_intake] no target knowledge base is available")
            return {
                "kb_name": "",
                "imported_count": 0,
                "failed_count": len(results),
                "error": "No target knowledge base is available.",
            }

        imported_count = 0
        failed_count = 0
        for item in results:
            if item.status in {"failed", "too_large", "unsupported"}:
                continue
            try:
                file_content = await asyncio.to_thread(item.stored_path.read_bytes)
                await helper.upload_document(
                    file_name=item.stored_path.name,
                    file_content=file_content,
                    file_type=item.stored_path.suffix.lower().lstrip("."),
                    source_path=str(item.stored_path),
                )
                logger.info(
                    "[document_intake] imported file=%s kb=%s",
                    item.stored_path,
                    kb_name,
                )
                imported_count += 1
            except Exception as exc:  # noqa: BLE001
                failed_count += 1
                logger.warning(
                    "[document_intake] kb import failed file=%s kb=%s: %s",
                    item.stored_path,
                    kb_name,
                    exc,
                )
        return {
            "kb_name": kb_name,
            "imported_count": imported_count,
            "failed_count": failed_count,
            "error": "" if failed_count == 0 else "Some files failed to import.",
        }
