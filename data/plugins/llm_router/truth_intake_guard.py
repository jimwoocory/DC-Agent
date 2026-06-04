from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dc_engines.employee_directory import requester_meta_from_event
from dc_engines.harness import HarnessTaskCreateRequest

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.message_components import File, Image, Record, Video

INTAKE_ROOT = Path("/Users/dianchi/DC-Agent/data/harness_intake")
INTAKE_SOURCE = "llm_router_truth_intake"
INTAKE_DOMAIN = "truth_intake"
INTAKE_PLATFORMS = {"巅池-Agent小助手"}
ACTIVE_INTAKE_STATUSES = ("blocked", "pending", "in_progress")

FEISHU_URL_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)?(?:feishu|larksuite)\.(?:cn|com)/\S+",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

FICTION_OR_TEMPLATE_RE = re.compile(
    r"(虚构|假设|模拟|示例|模板|占位|不需要真实|随便编一个|脑洞)",
)
FACT_SCOPE_RE = re.compile(
    r"(公司|巅池|DC-Agent|Hermes|Harness|客户|甲方|员工|老板|内部|项目|产品|品牌|"
    r"合同|报价|预算|财务|销量|数据|排期|进度|状态|公告|邮件|制度|文档|文件|"
    r"图片|截图|视频|素材|案例|官网|公众号|飞书|知识库|我们公司|咱们公司)",
    re.IGNORECASE,
)
FACT_ACTION_RE = re.compile(
    r"(写|生成|做|制作|设计|发|回复|整理|总结|分析|判断|确认|查询|查看|看看|"
    r"给出|输出|汇报|报告|方案|文案|公告|邮件|视频|图片|是否|是不是|有没有|"
    r"多少|完成|进度|状态|真实|准确|对不对|可不可以)",
)
DEEP_PREFIX_RE = re.compile(r"^\s*#(深度|洞察|创意|PRD|高|超深|codex)", re.IGNORECASE)
SUPPLEMENT_RE = re.compile(
    r"^\s*(补充|资料|材料|素材|原文|背景|参考|文件|图片|截图|链接|如下|给你|这是)",
)
SOURCE_MARKER_RE = re.compile(
    r"(资料|材料|素材|原文|背景|参考|如下|链接|截图|文件|图片|品牌规范|产品信息|"
    r"目标|受众|预算|排期|客户|甲方|官网|公众号|飞书|知识库)",
)
MEDIA_TASK_RE = re.compile(
    r"(海报|图片|图像|视频|动画|封面|视觉|物料|素材|生图|文生视频|图生视频)",
)
WRITING_TASK_RE = re.compile(r"(公告|邮件|文案|通知|话术|脚本|推文|公众号|介绍|汇报)")
ANALYSIS_TASK_RE = re.compile(r"(分析|洞察|报告|复盘|策略|方案|调研|判断|评估)")


@dataclass(slots=True)
class AttachmentArchive:
    kind: str
    original_name: str
    stored_path: str
    sha256: str
    size: int


@dataclass(slots=True)
class IntakeArchive:
    intake_id: str
    intake_dir: Path
    text_path: Path
    attachments: list[AttachmentArchive]
    kb_status: str = "not_scheduled"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _safe_name(name: str, fallback: str) -> str:
    candidate = (name or fallback).strip() or fallback
    candidate = re.sub(r"[^\w.\-]+", "_", candidate, flags=re.UNICODE)
    return candidate[:120] or fallback


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _platform_id(event: AstrMessageEvent) -> str:
    try:
        return event.get_platform_id() or ""
    except Exception:  # noqa: BLE001
        return ""


def _sender_id(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_sender_id() or "")
    except Exception:  # noqa: BLE001
        return ""


def _message_components(event: AstrMessageEvent) -> list:
    try:
        message = event.message_obj.message
    except Exception:  # noqa: BLE001
        return []
    return message if isinstance(message, list) else []


def _has_attachment(event: AstrMessageEvent) -> bool:
    return any(
        isinstance(comp, (Image, Record, File, Video))
        for comp in _message_components(event)
    )


def _has_source_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if FEISHU_URL_RE.search(stripped) or URL_RE.search(stripped):
        return True
    if SUPPLEMENT_RE.search(stripped):
        return True
    if SOURCE_MARKER_RE.search(stripped) and len(stripped) >= 80:
        return True
    if stripped.count("\n") >= 2 and len(stripped) >= 120:
        return True
    if "：" in stripped and len(stripped) >= 160:
        return True
    return False


def _needs_truth_evidence(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if FICTION_OR_TEMPLATE_RE.search(stripped):
        return False
    if not FACT_SCOPE_RE.search(stripped):
        return False
    if DEEP_PREFIX_RE.search(stripped):
        return True
    return bool(FACT_ACTION_RE.search(stripped))


def _required_materials(text: str) -> list[str]:
    needs = [
        "真实背景：这件事的对象、目的、上下文、时间范围",
        "可核验来源：飞书文档链接、原文、截图、文件、表格或图片",
    ]
    if MEDIA_TASK_RE.search(text):
        needs.append("媒体素材：品牌视觉、产品图、参考图、视频/图片要求、尺寸比例")
    if WRITING_TASK_RE.search(text):
        needs.append("文案边界：受众、语气、发布渠道、必须出现/不能出现的信息")
    if ANALYSIS_TASK_RE.search(text) or DEEP_PREFIX_RE.search(text):
        needs.append("分析依据：数据源、业务事实、已有结论、约束条件")
    return needs


def _replace_event_text(event: AstrMessageEvent, text: str) -> None:
    event.message_str = text
    try:
        event.message_obj.message_str = text
    except Exception:  # noqa: BLE001
        pass


async def _conversation_id(context: Any, event: AstrMessageEvent) -> str:
    umo = event.unified_msg_origin or ""
    manager = getattr(context, "conversation_manager", None)
    if manager is not None and umo:
        try:
            conv_id = await manager.get_curr_conversation_id(umo)
            if conv_id:
                return str(conv_id)
            conv_id = await manager.new_conversation(umo, _platform_id(event))
            if conv_id:
                return str(conv_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[truth_intake] conversation lookup failed: %s", exc)
    seed = umo or f"{_platform_id(event)}:{_sender_id(event)}"
    return "session-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


async def _find_blocked_intake_task(context: Any, event: AstrMessageEvent):
    store = getattr(context, "harness_store", None)
    if store is None:
        return None
    session_id = event.unified_msg_origin or ""
    if not session_id:
        return None
    try:
        tasks = await store.list_tasks_for_session(
            session_id,
            limit=10,
            statuses=ACTIVE_INTAKE_STATUSES,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[truth_intake] list active intake tasks failed: %s", exc)
        return None
    for task in tasks:
        payload = getattr(task, "payload", {}) or {}
        if (
            getattr(task, "domain", "") == INTAKE_DOMAIN
            and payload.get("source") == INTAKE_SOURCE
            and getattr(task, "status", "") == "blocked"
        ):
            return task
    return None


async def _create_intake_task(
    context: Any,
    event: AstrMessageEvent,
    *,
    text: str,
    status: str,
    intake_id: str,
    archive: IntakeArchive | None = None,
):
    engine = getattr(context, "harness_engine", None)
    if engine is None:
        return None

    payload = {
        "source": INTAKE_SOURCE,
        "truth_policy_version": "2026-05-20",
        "original_text": text[:4000],
        "required_materials": _required_materials(text),
        "sender_id": _sender_id(event),
        "intake_id": intake_id,
        "auto_complete_on_response": status == "in_progress",
    }
    payload.update(await requester_meta_from_event(context, event))
    if archive is not None:
        payload["archive_dir"] = str(archive.intake_dir)
        payload["attachments"] = [
            {
                "kind": a.kind,
                "original_name": a.original_name,
                "stored_path": a.stored_path,
                "sha256": a.sha256,
                "size": a.size,
            }
            for a in archive.attachments
        ]

    try:
        task = await engine.create_task(
            HarnessTaskCreateRequest(
                title=f"真实性资料校验：{text[:48]}",
                conversation_id=await _conversation_id(context, event),
                platform_id=_platform_id(event),
                session_id=event.unified_msg_origin or "",
                domain=INTAKE_DOMAIN,
                payload=payload,
            )
        )
        case_id = await _ensure_case_for_intake(context, event, task.task_id, text)
        await _link_inbox_for_intake(context, event, task.task_id, status, case_id)
        if status == "blocked":
            return await engine.set_status(
                task.task_id,
                "blocked",
                event_payload={
                    "reason": "missing_verifiable_source_materials",
                    "intake_id": intake_id,
                },
            )
        if status == "in_progress":
            return await engine.mark_in_progress(
                task.task_id,
                note="verifiable source materials received",
            )
        return task
    except Exception as exc:  # noqa: BLE001
        logger.warning("[truth_intake] create intake task failed: %s", exc)
        return None


async def _ensure_case_for_intake(
    context: Any,
    event: AstrMessageEvent,
    task_id: str,
    text: str,
) -> str:
    case_engine = getattr(context, "case_engine", None)
    if case_engine is None:
        return ""
    try:
        case = await case_engine.get_current_case_for_session(event.unified_msg_origin)
        if case is None:
            ensure_case = getattr(context, "ai_inbox_ensure_case", None)
            if ensure_case is not None:
                case_id = await ensure_case(
                    event,
                    category="material",
                    text=text,
                    task_id=task_id,
                )
                if case_id:
                    case = await case_engine.store.get_case(case_id)
        if case is not None:
            await case_engine.attach_task(case.case_id, task_id)
            return case.case_id
    except Exception as exc:  # noqa: BLE001
        logger.debug("[truth_intake] case attach skipped: %s", exc)
    return ""


async def _link_inbox_for_intake(
    context: Any,
    event: AstrMessageEvent,
    task_id: str,
    status: str,
    case_id: str,
) -> None:
    link_task = getattr(context, "ai_inbox_link_task", None)
    if link_task is None:
        return
    inbox_status = "waiting_materials" if status == "blocked" else "in_progress"
    try:
        await link_task(
            event,
            task_id,
            status=inbox_status,
            case_id=case_id,
            source=INTAKE_SOURCE,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[truth_intake] inbox link skipped: %s", exc)


async def _archive_attachment(comp: Any, target_dir: Path) -> AttachmentArchive | None:
    try:
        if isinstance(comp, Image):
            source = await comp.convert_to_file_path()
            kind = "image"
            original = Path(source).name if source else "image"
        elif isinstance(comp, Record):
            source = await comp.convert_to_file_path()
            kind = "record"
            original = Path(source).name if source else "record"
        elif isinstance(comp, Video):
            source = await comp.convert_to_file_path()
            kind = "video"
            original = Path(source).name if source else "video"
        elif isinstance(comp, File):
            source = await comp.get_file()
            kind = "file"
            original = comp.name or (Path(source).name if source else "file")
        else:
            return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("[truth_intake] attachment materialization failed: %s", exc)
        return None

    if not source:
        return None
    source_path = Path(source)
    if not source_path.exists() or not source_path.is_file():
        return None

    safe = _safe_name(original, f"{kind}_{uuid.uuid4().hex[:8]}")
    target = target_dir / safe
    if target.exists():
        target = target_dir / f"{target.stem}_{uuid.uuid4().hex[:8]}{target.suffix}"
    await asyncio.to_thread(shutil.copy2, source_path, target)
    digest = await asyncio.to_thread(_sha256_file, target)
    return AttachmentArchive(
        kind=kind,
        original_name=original,
        stored_path=str(target),
        sha256=digest,
        size=target.stat().st_size,
    )


async def _archive_source_materials(
    event: AstrMessageEvent,
    *,
    text: str,
    original_text: str,
    intake_id: str,
    blocked_task_id: str | None = None,
) -> IntakeArchive:
    intake_dir = INTAKE_ROOT / "raw" / _today() / intake_id
    attachments_dir = intake_dir / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)

    text_path = intake_dir / "message.txt"
    text_path.write_text(text, encoding="utf-8")

    attachments: list[AttachmentArchive] = []
    for comp in _message_components(event):
        if not isinstance(comp, (Image, Record, File, Video)):
            continue
        archived = await _archive_attachment(comp, attachments_dir)
        if archived is not None:
            attachments.append(archived)

    metadata = {
        "intake_id": intake_id,
        "source": INTAKE_SOURCE,
        "created_at": _utcnow(),
        "platform_id": _platform_id(event),
        "session_id": event.unified_msg_origin or "",
        "sender_id": _sender_id(event),
        "blocked_task_id": blocked_task_id,
        "original_text": original_text,
        "message_text": text,
        "attachments": [
            {
                "kind": a.kind,
                "original_name": a.original_name,
                "stored_path": a.stored_path,
                "sha256": a.sha256,
                "size": a.size,
            }
            for a in attachments
        ],
    }
    (intake_dir / "request.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    INTAKE_ROOT.mkdir(parents=True, exist_ok=True)
    with (INTAKE_ROOT / "intake_events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n")

    return IntakeArchive(
        intake_id=intake_id,
        intake_dir=intake_dir,
        text_path=text_path,
        attachments=attachments,
    )


def _source_context_block(
    *,
    archive: IntakeArchive,
    source_text: str,
    original_text: str,
    task_id: str | None,
) -> str:
    attachment_lines = "\n".join(
        f"- {a.kind}: {a.original_name} ({a.sha256[:12]}, {a.size} bytes)"
        for a in archive.attachments
    )
    if not attachment_lines:
        attachment_lines = "- none"
    task_line = f"task_id={task_id}" if task_id else "task_id=none"
    return (
        f'<dc_truth_source intake_id="{archive.intake_id}" {task_line}>\n'
        f"原始需求：{original_text.strip()[:1200]}\n\n"
        f"员工补充/提供的真实资料：\n{source_text.strip()[:5000]}\n\n"
        f"归档目录：{archive.intake_dir}\n"
        f"附件：\n{attachment_lines}\n\n"
        "真实性约束：只能基于用户提供的资料、附件摘要、已检索知识库或明确可验证来源回答；"
        "缺失的信息必须说明无法确认并继续要材料，不得编造公司事实、客户信息、员工信息、"
        "数据、进度、文件内容或工具执行结果。\n"
        "</dc_truth_source>"
    )


def _augment_event_with_source(
    event: AstrMessageEvent,
    *,
    original_text: str,
    source_text: str,
    archive: IntakeArchive,
    task_id: str | None,
) -> None:
    block = _source_context_block(
        archive=archive,
        source_text=source_text,
        original_text=original_text,
        task_id=task_id,
    )
    merged = f"{original_text.strip()}\n\n{block}" if original_text.strip() else block
    _replace_event_text(event, merged)
    event.set_extra("dc_truth_intake_id", archive.intake_id)
    if task_id:
        event.set_extra("dc_truth_intake_task_id", task_id)
    event.set_extra("dc_truth_source_archive", str(archive.intake_dir))


def _select_kb_names(text: str) -> tuple[str, ...]:
    if re.search(r"(品牌|视觉|logo|VI|海报|图片|素材|视频|营销|传播|公众号)", text):
        return ("营销素材", "品牌规范", "中台运营", "nas_knowledge")
    if re.search(r"(制度|员工|内部|流程|公告|邮件|项目|运营)", text):
        return ("中台运营", "nas_knowledge", "营销素材")
    return ("中台运营", "nas_knowledge", "营销素材")


async def _sync_archive_to_kb(context: Any, archive: IntakeArchive, text: str) -> None:
    kb_manager = getattr(context, "kb_manager", None)
    if kb_manager is None or not isinstance(
        getattr(kb_manager, "kb_insts", None), dict
    ):
        return

    helper = None
    kb_name = ""
    for name in _select_kb_names(text):
        try:
            helper = await kb_manager.get_kb_by_name(name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[truth_intake] kb lookup failed name=%s: %s", name, exc)
            helper = None
        if helper is not None:
            kb_name = name
            break
    if helper is None:
        return

    doc_text = (
        f"# Harness Intake {archive.intake_id}\n\n"
        f"归档目录：{archive.intake_dir}\n\n"
        f"原始资料：\n{text.strip()}\n"
    )
    if archive.attachments:
        doc_text += "\n附件：\n" + "\n".join(
            f"- {a.kind}: {a.original_name} sha256={a.sha256} path={a.stored_path}"
            for a in archive.attachments
        )
    try:
        await helper.upload_document(
            file_name=f"harness-intake-{archive.intake_id}.md",
            file_content=None,
            file_type="md",
            pre_chunked_text=[doc_text],
        )
        logger.info(
            "[truth_intake] synced intake=%s to kb=%s",
            archive.intake_id,
            kb_name,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[truth_intake] kb sync failed intake=%s kb=%s: %s",
            archive.intake_id,
            kb_name,
            exc,
        )


def _schedule_kb_sync(context: Any, archive: IntakeArchive, text: str) -> None:
    kb_manager = getattr(context, "kb_manager", None)
    if kb_manager is None or not isinstance(
        getattr(kb_manager, "kb_insts", None), dict
    ):
        return
    try:
        asyncio.create_task(_sync_archive_to_kb(context, archive, text))
        archive.kb_status = "scheduled"
    except RuntimeError:
        archive.kb_status = "no_running_loop"


def _missing_material_reply(text: str, task_id: str | None) -> str:
    required = "\n".join(f"- {item}" for item in _required_materials(text))
    task_line = f"\nHarness 已记录资料待补任务：#{task_id[:8]}" if task_id else ""
    return (
        "我先帮你把这件事稳住。这个请求涉及公司真实事实或真实素材，"
        "为了不让内容失真，我还需要一点可核验的资料。"
        f"{task_line}\n\n"
        "你方便补充下面这些信息吗？有多少给多少，我会接着往下处理：\n"
        f"{required}\n\n"
        "可以直接贴原文、发文件/图片/截图，或给飞书文档/知识库链接。"
    )


async def maybe_handle_truth_intake(
    context: Any,
    event: AstrMessageEvent,
    cfg: dict[str, Any] | None = None,
) -> bool:
    if cfg is not None and not bool(cfg.get("enabled", False)):
        return False
    dry_run = bool(cfg.get("dry_run", False)) if cfg is not None else False

    platform_id = _platform_id(event)
    if platform_id not in INTAKE_PLATFORMS:
        return False

    text = (event.message_str or "").strip()
    if not text:
        return False

    blocked_task = await _find_blocked_intake_task(context, event)
    has_source = _has_source_text(text) or _has_attachment(event)
    needs_evidence = _needs_truth_evidence(text)

    if blocked_task is not None and has_source:
        original_text = str((blocked_task.payload or {}).get("original_text") or "")
        intake_id = uuid.uuid4().hex
        archive = await _archive_source_materials(
            event,
            text=text,
            original_text=original_text,
            intake_id=intake_id,
            blocked_task_id=blocked_task.task_id,
        )
        engine = getattr(context, "harness_engine", None)
        if engine is None:
            logger.debug("[truth_intake] harness engine unavailable; continue")
            return False
        try:
            material_patch = {
                "latest_intake_id": intake_id,
                "archive_dir": str(archive.intake_dir),
                "attachments": [
                    {
                        "kind": a.kind,
                        "original_name": a.original_name,
                        "stored_path": a.stored_path,
                        "sha256": a.sha256,
                        "size": a.size,
                    }
                    for a in archive.attachments
                ],
            }
            merge_payload = getattr(engine, "merge_payload", None)
            if callable(merge_payload):
                await merge_payload(
                    blocked_task.task_id,
                    material_patch,
                    event_type="truth_materials_payload_attached",
                )
            await engine.append_trace(
                blocked_task.task_id,
                "truth_materials_received",
                {
                    "intake_id": intake_id,
                    "archive_dir": str(archive.intake_dir),
                    "attachments": len(archive.attachments),
                },
            )
            await engine.mark_in_progress(
                blocked_task.task_id,
                note="source materials received; router may continue",
            )
            case_id = await _ensure_case_for_intake(
                context,
                event,
                blocked_task.task_id,
                text,
            )
            await _link_inbox_for_intake(
                context,
                event,
                blocked_task.task_id,
                "in_progress",
                case_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[truth_intake] unblock intake task failed: %s", exc)
            return False
        _schedule_kb_sync(context, archive, text)
        if dry_run:
            logger.info(
                "[truth_intake] dry-run resumed blocked task=%s intake=%s",
                blocked_task.task_id[:8],
                intake_id,
            )
            return False
        _augment_event_with_source(
            event,
            original_text=original_text or text,
            source_text=text,
            archive=archive,
            task_id=blocked_task.task_id,
        )
        logger.info(
            "[truth_intake] resumed blocked task=%s intake=%s",
            blocked_task.task_id[:8],
            intake_id,
        )
        return False

    if not needs_evidence:
        return False

    if not has_source:
        intake_id = uuid.uuid4().hex
        task = await _create_intake_task(
            context,
            event,
            text=text,
            status="blocked",
            intake_id=intake_id,
        )
        task_id = getattr(task, "task_id", None)
        if task is None:
            logger.debug("[truth_intake] intake task unavailable; continue")
            return False
        if dry_run:
            logger.info(
                "[truth_intake] dry-run would block missing materials platform=%s task=%s text=%r",
                platform_id,
                (task_id or "-")[:8],
                text[:80],
            )
            return False
        event.should_call_llm(False)
        event.set_result(
            MessageEventResult()
            .message(_missing_material_reply(text, task_id))
            .use_t2i(False)
            .stop_event()
        )
        logger.info(
            "[truth_intake] blocked message for missing materials platform=%s task=%s text=%r",
            platform_id,
            (task_id or "-")[:8],
            text[:80],
        )
        return True

    intake_id = uuid.uuid4().hex
    archive = await _archive_source_materials(
        event,
        text=text,
        original_text=text,
        intake_id=intake_id,
    )
    task = await _create_intake_task(
        context,
        event,
        text=text,
        status="in_progress",
        intake_id=intake_id,
        archive=archive,
    )
    task_id = getattr(task, "task_id", None)
    if task is None:
        logger.debug("[truth_intake] intake task unavailable; continue")
        return False
    _schedule_kb_sync(context, archive, text)
    if dry_run:
        logger.info(
            "[truth_intake] dry-run archived source materials intake=%s task=%s attachments=%d",
            intake_id,
            (task_id or "-")[:8],
            len(archive.attachments),
        )
        return False
    _augment_event_with_source(
        event,
        original_text=text,
        source_text=text,
        archive=archive,
        task_id=task_id,
    )
    logger.info(
        "[truth_intake] archived source materials intake=%s task=%s attachments=%d",
        intake_id,
        (task_id or "-")[:8],
        len(archive.attachments),
    )
    return False
