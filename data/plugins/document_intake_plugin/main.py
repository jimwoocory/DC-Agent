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


@dataclass(frozen=True)
class IntakeResult:
    original_name: str
    stored_path: Path
    sha256: str
    size_bytes: int
    parsed_text: str
    status: str
    error: str = ""


def _replace_event_text(event: AstrMessageEvent, text: str) -> None:
    event.message_str = text
    try:
        event.message_obj.message_str = text
    except Exception:  # noqa: BLE001
        pass


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
        if ready_results and self.auto_import:
            try:
                asyncio.create_task(self._upload_results_to_kb(ready_results))
            except RuntimeError as exc:
                logger.warning("[document_intake] cannot schedule kb import: %s", exc)

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

    async def _upload_results_to_kb(self, results: list[IntakeResult]) -> None:
        kb_manager = getattr(self.context, "kb_manager", None)
        if kb_manager is None:
            return
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
            return

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
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[document_intake] kb import failed file=%s kb=%s: %s",
                    item.stored_path,
                    kb_name,
                    exc,
                )
