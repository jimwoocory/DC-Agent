"""Deterministic Feishu assistant workbench routing."""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from dc_engines.assistant_workbench_cards import (
    TASK_FIELD_LABELS,
    TASK_LABELS,
    TASK_REQUIRED_FIELDS,
    TASK_VALUE_LABELS,
    build_assistant_task_confirmation_card,
    build_assistant_task_detail_card,
    build_assistant_task_intake_card,
    build_assistant_task_list_card,
    build_assistant_task_workspace_card,
    build_codex_toolbox_card,
)
from dc_engines.codex_capability import authorize_codex

from astrbot.api import logger
from astrbot.api.event import MessageEventResult

from ..middle_router_adapter import route_capability_request

WORKBENCH_MENU_ALIASES = frozenset({"工作台", "开始工作", "当前状态", "打开工作台"})
TASK_INTAKE_MENU_ALIASES = frozenset({"发起任务", "媒体生成", "图片/视频"})
TASK_LIST_MENU_ALIASES = frozenset({"我的任务", "进行中", "待补充", "已完成"})
RESUME_MENU_ALIASES = frozenset({"继续最近", "继续上次任务"})
CODEX_TOOL_MENU_ALIASES = frozenset(
    {"Codex 高级工具", "Codex 工具", "高级工具", "打开 Codex"}
)
LEGACY_TASK_MENU_PRESETS = {
    "文案资料": "copy",
    "写文案/方案": "copy",
    "生成图片": "image",
    "生成视频": "video",
    "查资料/分析": "research",
    "处理文件": "file",
    "物料报价": "quotation",
    "筹备组物料报价": "quotation",
    "报价系统": "quotation",
    "AI转CDR": "ai_cdr",
    "AI 转 CDR": "ai_cdr",
    "AI转CDR工具": "ai_cdr",
}

TASK_STATUS_FILTERS = {
    "进行中": "active",
    "待补充": "pending",
    "已完成": "completed",
}

TASK_STATUS_LABELS = {
    "pending": "等待执行",
    "in_progress": "进行中",
    "blocked": "待补充",
    "review_required": "待确认",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}


@dataclass(slots=True)
class WorkbenchActionResult:
    """Result returned to the shared card-action router.

    Attributes:
        handled: Whether the workbench consumed the card callback.
        resumed_text: Normal user text to route only after explicit execution.
    """

    handled: bool
    resumed_text: str = ""


def _stop_without_llm(event: Any, fallback_text: str = "") -> None:
    """Stop the event without invoking a model.

    Args:
        event: AstrBot message event.
        fallback_text: Text response used when a card cannot be sent.
    """
    event.should_call_llm(False)
    if fallback_text:
        event.set_result(
            MessageEventResult().message(fallback_text).use_t2i(False).stop_event()
        )
        return
    event.stop_event()


async def _load_task_rows(
    context: Any,
    event: Any,
    *,
    status_filter: str = "",
) -> list[dict[str, str]]:
    """Load truthful task rows from the existing Harness store.

    Args:
        context: AstrBot runtime context containing the Harness store.
        event: Current message or trusted card callback event.
        status_filter: Optional active, pending, or completed filter.

    Returns:
        Up to twenty task dictionaries safe for card rendering. Returns an
        empty list when the store or session identity is unavailable.
    """
    store = getattr(context, "harness_store", None)
    if store is None:
        engine = getattr(context, "harness_engine", None)
        store = getattr(engine, "store", None)
    list_for_session = getattr(store, "list_tasks_for_session", None)
    session_id = str(getattr(event, "unified_msg_origin", "") or "")
    if not callable(list_for_session) or not session_id:
        return []
    try:
        tasks = await list_for_session(session_id, limit=20)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc_router] task center load failed: %s", exc)
        return []
    allowed_statuses = {
        "active": {"pending", "in_progress"},
        "pending": {"blocked", "review_required"},
        "completed": {"completed"},
    }.get(status_filter)
    rows: list[dict[str, str]] = []
    for task in tasks:
        status = str(getattr(task, "status", "") or "")
        if allowed_statuses is not None and status not in allowed_statuses:
            continue
        rows.append(
            {
                "task_id": str(getattr(task, "task_id", "") or ""),
                "title": str(getattr(task, "title", "") or "未命名任务"),
                "status": status,
                "status_label": TASK_STATUS_LABELS.get(status, status or "未知"),
                "updated_at": str(getattr(task, "updated_at", "") or ""),
            }
        )
    return rows


def _task_resume_text(task_type: str, task_data: dict[str, str]) -> str:
    """Render confirmed task data for audit and downstream task instructions.

    Args:
        task_type: Normalized task category.
        task_data: Sanitized dedicated-form values.

    Returns:
        Text that preserves visible task parameters without encoding route triggers.
    """
    if task_type == "image":
        prompt = task_data.get("visual_prompt", "")
        lines = [f"图片任务目标：{prompt}", "任务类型：图片创作"]
    elif task_type == "video":
        prompt = task_data.get("video_prompt", "")
        mode = TASK_VALUE_LABELS.get(
            task_data.get("generation_mode", ""),
            task_data.get("generation_mode", ""),
        )
        lines = [
            f"视频任务目标：{prompt}",
            f"生成方式：{mode}",
            "任务类型：视频创作",
        ]
    elif task_type == "copy":
        prompt = task_data.get("task_request", "")
        lines = [
            f"#创意 {prompt}",
            "请直接输出成稿，不要检索或调用工具；未提供的具体数据、价格和承诺不要编造。",
            "任务类型：写文案 / 方案",
        ]
    elif task_type == "research":
        prompt = task_data.get("research_question", "")
        analysis_mode = TASK_VALUE_LABELS.get(
            task_data.get("analysis_mode", "evidence_review"), "证据查证"
        )
        depth = TASK_VALUE_LABELS.get(
            task_data.get("research_depth", "standard"), "标准分析"
        )
        source_policy = task_data.get("source_policy", "mixed")
        source_instruction = {
            "uploaded_only": "只使用本次上传、关联或明确提供的资料，不补充外部信息。",
            "public_only": "检索可靠的公开来源，优先官方、原始和可核验资料。",
            "mixed": "先使用本次资料，再用可靠公开来源补充和交叉验证。",
        }.get(source_policy, "先使用本次资料，再用可靠公开来源交叉验证。")
        structure_instruction = {
            "short_answer": "先给直接结论，再用精简证据说明，不展开无关背景。",
            "comparison": "使用同口径 Markdown 对比表，再给差异解释和选择建议。",
            "analysis_memo": "按结论、关键发现、证据、不确定性和下一步写分析备忘录。",
            "recommendation": "按建议、依据、优先级、风险和执行步骤形成方案。",
        }.get(task_data.get("output_format", "short_answer"), "先结论后证据。")
        lines = [
            f"研究分析任务：{prompt}",
            f"AI 分析方法：{analysis_mode}；分析深度：{depth}。",
            source_instruction,
            structure_instruction,
            "必须先理解已附资料再分析；关键事实标注文件名、页码、工作表或公开来源链接。",
            "明确区分已知事实、合理推断、建议和暂时无法确认的信息，不得编造来源或数据。",
            "最终只输出可直接交付的 Markdown 正文；文件生成由后续交付引擎完成。",
            "任务类型：LLM 研究分析",
        ]
    elif task_type == "file":
        prompt = task_data.get("file_goal", "")
        operation = task_data.get("operation", "summarize")
        operation_instruction = {
            "summarize": "提炼核心结论、关键数据、风险、决定和待办，不遗漏影响判断的重要限定。",
            "extract": "按用户要求的字段逐项提取；缺失项明确标空，并标注原文位置。",
            "rewrite": "保留原始事实、数据和承诺边界，按目标受众与用途重写成可用成稿。",
            "convert": "提取有效内容并按目标文件结构重新排版，不声称保留无法验证的原版式。",
            "compare": "对多份文件做同口径比较，列出相同点、差异、冲突、缺失和可能影响。",
            "organize": "按清晰分类整理内容，保留来源映射，重复项合并但不丢失差异。",
        }.get(operation, "按用户目标处理文件，并保留可追溯来源。")
        layout_instruction = (
            "尽量沿用原文件的标题层级和顺序。"
            if task_data.get("preserve_layout") == "preserve"
            else "按任务目标重新组织结构，以结果可用性优先。"
        )
        lines = [
            f"文件处理任务：{prompt}",
            operation_instruction,
            layout_instruction,
            "必须先读取本次上传或关联的真实文件；原文件没有的信息不得补写为事实。",
            "涉及表格时输出规范 Markdown 表格；涉及差异时标注对应文件、页码或工作表。",
            "最终只输出处理后的可交付正文；Word、Excel、PDF、文本或飞书云文档由后续交付引擎生成。",
            "任务类型：LLM 文件处理",
        ]
    elif task_type == "quotation":
        project_name = task_data.get("project_name", "未命名项目")
        lines = [
            "市场部估价结果",
            f"项目：{project_name}",
            f"客户 / 品牌：{task_data.get('client_name', '未填写')}",
            f"估价金额：¥{task_data.get('grand_total', '0.00')}",
            f"有效期：{task_data.get('validity_days', '15')} 天",
            f"状态：{task_data.get('quotation_status', '估价方案')}",
            "口径：该金额为筹备组基于历史价格完成的项目估价，仅供市场部制定方案使用；不展示内部测算明细。",
        ]
        return "\n".join(lines)
    elif task_type == "codex":
        prompt = task_data.get("task_request", "")
        lines = [f"#codex工具 {prompt}", "任务类型：Codex 高级处理（只读）"]
    else:
        lines = [f"任务类型：{TASK_LABELS.get(task_type, TASK_LABELS['copy'])}"]
    for key, value in task_data.items():
        if (
            not value
            or key in {"visual_prompt", "video_prompt"}
            or (task_type in {"copy", "codex"} and key == "task_request")
            or (task_type == "research" and key == "research_question")
            or (task_type == "file" and key == "file_goal")
        ):
            continue
        if task_type == "image" and key == "model_choice":
            label = "生图模型"
            display_value = {
                "auto": "自动（Image2 优先，即梦兜底）",
                "image2": "Image2（仅使用）",
                "dreamina": "即梦（仅使用）",
            }.get(value, value)
        else:
            label = TASK_FIELD_LABELS.get(key, key)
            display_value = TASK_VALUE_LABELS.get(value, value)
        lines.append(f"{label}：{display_value}")
    return "\n".join(lines)


async def _send_card(
    context: Any,
    event: Any,
    *,
    card_type: str,
    card: dict[str, Any],
    replace_message_id: str = "",
) -> Any | None:
    """Send or replace one registered workbench card through the runtime.

    Args:
        context: AstrBot runtime context containing Feishu streamers.
        event: Current Feishu message or trusted card callback event.
        card_type: Registered workbench card type.
        card: Card JSON payload.
        replace_message_id: Existing card message kept for chain navigation.

    Returns:
        Runtime stream when the card send or patch succeeds, otherwise None.
    """
    platform_name = str(getattr(event, "get_platform_name", lambda: "")() or "").lower()
    if platform_name != "lark":
        return None

    try:
        from dc_engines.card_runtime import (
            patch_card_via_runtime,
            send_card_via_runtime,
        )
        from dc_engines.feishu_card_streamer import (
            ensure_streamers_on_context,
            extract_chat_info_from_event,
        )

        platform_id = event.get_platform_id() or ""
        streamers = ensure_streamers_on_context(context)
        streamer = streamers.get(platform_id)
        if streamer is None:
            from dc_engines.feishu_card_streamer import FeishuCardStreamer

            platform_manager = getattr(context, "platform_manager", None)
            platform_insts = getattr(platform_manager, "platform_insts", None) or []
            for inst in platform_insts:
                lark_api = getattr(inst, "lark_api", None)
                if lark_api is None:
                    continue
                streamer = FeishuCardStreamer(lark_api)
                streamers[platform_id] = streamer
                context.feishu_streamers = streamers
                logger.info(
                    "[dc_router] assistant card streamer rebuilt platform=%s",
                    platform_id,
                )
                break
        if streamer is None:
            logger.warning(
                "[dc_router] assistant card streamer unavailable platform=%s",
                platform_id,
            )
            return None

        payload = getattr(
            getattr(event, "message_obj", None), "card_action_payload", None
        )
        payload = payload if isinstance(payload, dict) else {}
        chat_id = str(payload.get("open_chat_id") or "")
        if chat_id:
            receive_id_type = "chat_id"
        else:
            chat_id, receive_id_type = extract_chat_info_from_event(event)
        if not chat_id:
            logger.warning(
                "[dc_router] assistant card chat identity unavailable platform=%s",
                platform_id,
            )
            return None

        if replace_message_id:
            patched = await patch_card_via_runtime(
                streamer,
                card_type=card_type,
                message_id=replace_message_id,
                card=card,
                platform_id=platform_id,
                chat_id=chat_id,
                receive_id_type=receive_id_type,
                event="navigation_patch",
                detail="deterministic assistant workbench navigation",
            )
            if not patched:
                return None
            stream = streamer.get_stream(replace_message_id)
            if stream is not None:
                return stream
            from dc_engines.feishu_card_streamer import CardStream

            return CardStream(
                message_id=replace_message_id,
                chat_id=chat_id,
                receive_id_type=receive_id_type,
                last_card=card,
            )

        stream = await send_card_via_runtime(
            streamer,
            card_type=card_type,
            chat_id=chat_id,
            receive_id_type=receive_id_type,
            card=card,
            platform_id=platform_id,
            event="start",
            detail="deterministic assistant workbench navigation",
        )
        return stream
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] workbench card send failed: %s", exc)
        return None


def _workspace_base_url() -> str:
    """Resolve the H5 origin reachable from the Feishu desktop client.

    Returns:
        Configured public callback origin or a LAN dashboard origin.
    """
    configured = os.environ.get("DC_ASSISTANT_H5_ORIGIN", "").strip().rstrip("/")
    try:
        from astrbot.core import astrbot_config

        if not configured:
            configured = str(astrbot_config.get("callback_api_base", "") or "").strip()
        port = int(astrbot_config.get("dashboard", {}).get("port", 6185))
    except Exception:  # noqa: BLE001
        port = 6185
    parsed = urlsplit(configured)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return configured.rstrip("/")
    local_ip = "127.0.0.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            local_ip = str(probe.getsockname()[0] or local_ip)
    except OSError:
        pass
    return f"http://{local_ip}:{port}"


async def _send_task_workspace(
    context: Any,
    event: Any,
    *,
    task_type: str,
    replace_message_id: str = "",
) -> bool:
    """Create, send, and bind one task-aware Feishu sidebar workspace.

    Args:
        context: AstrBot runtime context containing Feishu platforms.
        event: Current message or trusted card callback event.
        task_type: Selected workbench task category.
        replace_message_id: Existing task-chain card to replace in place.

    Returns:
        True after the capability is bound to the newly sent card.
    """
    normalized_type = task_type if task_type in TASK_LABELS else "copy"
    platform_id = str(event.get_platform_id() or "")
    platform = None
    get_platform_inst = getattr(context, "get_platform_inst", None)
    if callable(get_platform_inst):
        try:
            platform = get_platform_inst(platform_id)
        except Exception:  # noqa: BLE001
            platform = None
    if platform is None:
        platform_manager = getattr(context, "platform_manager", None)
        for item in getattr(platform_manager, "platform_insts", None) or []:
            if getattr(item, "lark_api", None) is not None:
                platform = item
                break
    config = getattr(platform, "config", None) or {}
    app_id = str(config.get("app_id") or "") if isinstance(config, dict) else ""
    if not app_id:
        logger.warning(
            "[dc_router] task workspace app id unavailable platform=%s",
            platform_id,
        )
        return False
    event_session_id = getattr(event, "session_id", "")
    session_id = event_session_id if isinstance(event_session_id, str) else ""
    message_type_value = ""
    if callable(getattr(event, "get_message_type", None)):
        message_type_value = getattr(event.get_message_type(), "value", "")
    message_type = (
        message_type_value
        if message_type_value in {"FriendMessage", "GroupMessage", "OtherMessage"}
        else "FriendMessage"
    )
    sender_id_value = (
        event.get_sender_id() if callable(getattr(event, "get_sender_id", None)) else ""
    )
    sender_name_value = (
        event.get_sender_name()
        if callable(getattr(event, "get_sender_name", None))
        else ""
    )
    group_id_value = (
        event.get_group_id() if callable(getattr(event, "get_group_id", None)) else ""
    )
    workspace_origin = _workspace_base_url()
    origin = urlsplit(workspace_origin)
    use_remote_store = (origin.hostname or "").lower() not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    admin_token = os.environ.get("DC_ASSISTANT_H5_ADMIN_TOKEN", "").strip()
    remote_client: httpx.AsyncClient | None = None
    try:
        from astrbot.dashboard.api.assistant_attachments import draft_store

        if use_remote_store:
            if not admin_token:
                raise RuntimeError(
                    "DC_ASSISTANT_H5_ADMIN_TOKEN is required for the NAS H5 origin"
                )
            remote_client = httpx.AsyncClient(timeout=8.0, trust_env=False)
            create_response = await remote_client.post(
                f"{workspace_origin}/api/v1/assistant-attachments/drafts",
                headers={"X-DC-Assistant-Admin-Token": admin_token},
                json={
                    "platform_id": platform_id,
                    "upload_base_url": workspace_origin,
                    "task_type": normalized_type,
                    "session_id": session_id,
                    "message_type": message_type,
                    "sender_id": (
                        sender_id_value if isinstance(sender_id_value, str) else ""
                    ),
                    "sender_name": (
                        sender_name_value if isinstance(sender_name_value, str) else ""
                    ),
                    "group_id": (
                        group_id_value if isinstance(group_id_value, str) else ""
                    ),
                },
            )
            create_response.raise_for_status()
            remote_draft = create_response.json()
            draft_token = str(remote_draft.get("token") or "")
            workspace_url = str(remote_draft.get("upload_url") or "")
            parsed_workspace = urlsplit(workspace_url)
            expected_path = (
                f"/api/v1/assistant-attachments/{draft_token}" if draft_token else ""
            )
            if (
                not draft_token
                or parsed_workspace.scheme != origin.scheme
                or parsed_workspace.netloc != origin.netloc
                or parsed_workspace.path != expected_path
                or parsed_workspace.query
                or parsed_workspace.fragment
            ):
                raise RuntimeError("NAS returned an invalid assistant workspace URL")
        else:
            draft = draft_store.create(
                platform_id=platform_id,
                upload_base_url=workspace_origin,
                task_type=normalized_type,
                session_id=session_id,
                message_type=message_type,
                sender_id=(sender_id_value if isinstance(sender_id_value, str) else ""),
                sender_name=(
                    sender_name_value if isinstance(sender_name_value, str) else ""
                ),
                group_id=group_id_value if isinstance(group_id_value, str) else "",
            )
            draft_token = draft.token
            workspace_url = draft.upload_url
        card = build_assistant_task_workspace_card(
            task_type=normalized_type,
            app_id=app_id,
            workspace_url=workspace_url,
        )
        stream = await _send_card(
            context,
            event,
            card_type="assistant_task_intake",
            card=card,
            replace_message_id=replace_message_id,
        )
        message_id = str(getattr(stream, "message_id", "") or "")
        if not message_id:
            return False
        if use_remote_store:
            bind_response = await remote_client.post(
                f"{workspace_origin}/api/v1/assistant-attachments/"
                f"drafts/{draft_token}/bind",
                headers={"X-DC-Assistant-Admin-Token": admin_token},
                json={"message_id": message_id},
            )
            bind_response.raise_for_status()
        else:
            draft_store.bind_message(draft_token, message_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] task workspace send failed: %s", exc)
        return False
    finally:
        if remote_client is not None:
            await remote_client.aclose()


async def try_handle_assistant_workbench(
    context: Any,
    event: Any,
    text: str,
) -> bool:
    """Handle exact Feishu menu labels without entering LLM routing.

    Args:
        context: AstrBot runtime context.
        event: Current Feishu message event.
        text: Plain message text produced by a menu click.

    Returns:
        True when a workbench menu alias was handled.
    """
    normalized = str(text or "").strip()
    capability_id = (
        f"workspace.{LEGACY_TASK_MENU_PRESETS[normalized]}"
        if normalized in LEGACY_TASK_MENU_PRESETS
        else "ui.workbench"
    )
    known_menu = normalized in (
        CODEX_TOOL_MENU_ALIASES
        | WORKBENCH_MENU_ALIASES
        | TASK_INTAKE_MENU_ALIASES
        | TASK_LIST_MENU_ALIASES
        | RESUME_MENU_ALIASES
        | frozenset(LEGACY_TASK_MENU_PRESETS)
    )
    if not known_menu:
        return False
    route = route_capability_request(
        event,
        source="menu",
        capability_id=capability_id,
        goal=normalized,
        confidence=1.0,
        action_force="navigate",
        trusted=True,
    )
    if not route.allowed:
        _stop_without_llm(event, "该工作台入口暂时不可用，请稍后重试。")
        return True
    card_type = ""
    card: dict[str, Any] | None = None
    fallback = ""

    if normalized in CODEX_TOOL_MENU_ALIASES:
        card_type = "assistant_task_intake"
        card = build_codex_toolbox_card()
        fallback = "Codex 高级工具已打开。"
    elif normalized in WORKBENCH_MENU_ALIASES or normalized in TASK_LIST_MENU_ALIASES:
        status_filter = TASK_STATUS_FILTERS.get(normalized, "")
        task_rows = await _load_task_rows(
            context,
            event,
            status_filter=status_filter,
        )
        card_type = "assistant_task_list"
        card = build_assistant_task_list_card(
            tasks=task_rows,
            status_filter=status_filter,
            has_recent_task=bool(task_rows),
        )
        fallback = "当前没有可展示的真实任务。"
    elif normalized in TASK_INTAKE_MENU_ALIASES:
        card_type = "assistant_task_intake"
        card = build_assistant_task_intake_card(
            task_type="media" if normalized in {"媒体生成", "图片/视频"} else ""
        )
        fallback = "请选择任务类型。"
    elif normalized in RESUME_MENU_ALIASES:
        task_rows = await _load_task_rows(context, event, status_filter="active")
        if not task_rows:
            _stop_without_llm(event, "当前没有可继续的最近任务。")
            return True
        card_type = "assistant_task_list"
        card = build_assistant_task_list_card(
            tasks=task_rows[:1],
            status_filter="active",
            has_recent_task=True,
        )
        fallback = "已打开最近任务。"
    elif normalized in LEGACY_TASK_MENU_PRESETS:
        task_type = LEGACY_TASK_MENU_PRESETS[normalized]
        sent = await _send_task_workspace(
            context,
            event,
            task_type=task_type,
        )
        _stop_without_llm(
            event,
            "" if sent else f"{TASK_LABELS[task_type]}工作台暂时无法打开。",
        )
        return True
    else:
        return False

    if card is None:
        return True

    sent = await _send_card(
        context,
        event,
        card_type=card_type,
        card=card,
    )
    _stop_without_llm(event, "" if sent else fallback)
    return True


async def handle_assistant_workbench_card_action(
    context: Any,
    event: Any,
    *,
    value: dict[str, Any],
    payload: dict[str, Any],
) -> WorkbenchActionResult:
    """Handle trusted workbench navigation, form preview and execution.

    Args:
        context: AstrBot runtime context.
        event: Trusted Feishu card callback event.
        value: Callback value configured on the clicked component.
        payload: Full normalized card callback payload.

    Returns:
        Navigation callbacks are handled locally. Explicit execution returns
        resumed text for the normal agent pipeline.
    """
    action = str(value.get("action") or "")
    replace_message_id = str(payload.get("open_message_id") or "").strip()
    card_type = ""
    card: dict[str, Any] | None = None
    fallback = ""

    if action in {
        "open_home",
        "open_task_intake",
        "open_task_list",
        "preview_task",
        "resume_recent",
        "show_task",
    }:
        route = route_capability_request(
            event,
            source="card",
            capability_id="ui.workbench",
            goal=action,
            confidence=1.0,
            action_force="navigate",
            trusted=True,
        )
        if not route.allowed:
            _stop_without_llm(event, "该卡片操作未通过路由校验。")
            return WorkbenchActionResult(handled=True)

    if action in {"open_new_session", "continue_current_session"}:
        from .session_choice import handle_session_choice_action

        choice = await handle_session_choice_action(
            context,
            event,
            value=value,
            payload=payload,
        )
        _stop_without_llm(event, choice.message)
        return WorkbenchActionResult(handled=choice.handled)

    if action == "open_home":
        task_rows = await _load_task_rows(context, event)
        card_type = "assistant_task_list"
        card = build_assistant_task_list_card(
            tasks=task_rows,
            has_recent_task=bool(task_rows),
        )
        fallback = "任务中心已打开。"
    elif action == "open_task_intake":
        card_type = "assistant_task_intake"
        card = build_assistant_task_intake_card()
        fallback = "请选择任务类型。"
    elif action == "open_task_list":
        status_filter = str(value.get("status_filter") or "")
        task_rows = await _load_task_rows(
            context,
            event,
            status_filter=status_filter,
        )
        card_type = "assistant_task_list"
        card = build_assistant_task_list_card(
            tasks=task_rows,
            status_filter=status_filter,
            has_recent_task=bool(task_rows),
        )
        fallback = "当前没有可展示的真实任务。"
    elif action == "select_task_type":
        task_type = str(value.get("task_type") or "copy")
        route = route_capability_request(
            event,
            source="card",
            capability_id=f"workspace.{task_type}",
            goal=f"打开{task_type}工作台",
            confidence=1.0,
            action_force="navigate",
            trusted=True,
        )
        if not route.allowed:
            _stop_without_llm(event, "该工作台入口未通过路由校验。")
            return WorkbenchActionResult(handled=True)
        sent = await _send_task_workspace(
            context,
            event,
            task_type=task_type,
            replace_message_id=replace_message_id,
        )
        _stop_without_llm(
            event,
            ""
            if sent
            else f"{TASK_LABELS.get(task_type, TASK_LABELS['copy'])}工作台暂时无法打开。",
        )
        return WorkbenchActionResult(handled=True)
    elif action == "preview_task":
        form_value = payload.get("form_value", {})
        form_value = form_value if isinstance(form_value, dict) else {}
        task_type = str(value.get("task_type") or "copy")
        task_type = task_type if task_type in TASK_LABELS else "copy"
        task_data = {
            str(key): str(item or "").strip()[
                : 6000
                if str(key)
                in {
                    "glossary",
                    "quotation_items",
                    "task_request",
                    "translation_memory",
                    "video_prompt",
                    "visual_prompt",
                }
                else 800
            ]
            for key, item in form_value.items()
            if str(item or "").strip()
        }
        required_field = TASK_REQUIRED_FIELDS[task_type]
        if not task_data.get(required_field):
            required_label = TASK_FIELD_LABELS.get(required_field, "必填信息")
            _stop_without_llm(event, f"请先填写{required_label}。")
            return WorkbenchActionResult(handled=True)
        card_type = "assistant_task_confirmation"
        card = build_assistant_task_confirmation_card(
            task_type=task_type,
            task_data=task_data,
        )
        fallback = "任务已进入确认页；目前尚未调用 LLM。"
    elif action == "start_task":
        task_type = str(value.get("task_type") or "copy")
        task_type = task_type if task_type in TASK_LABELS else "copy"
        raw_task_data = value.get("task_data", {})
        raw_task_data = raw_task_data if isinstance(raw_task_data, dict) else {}
        task_data = {
            str(key): str(item or "").strip()[
                : 6000
                if str(key)
                in {
                    "glossary",
                    "quotation_items",
                    "task_request",
                    "translation_memory",
                    "video_prompt",
                    "visual_prompt",
                }
                else 800
            ]
            for key, item in raw_task_data.items()
            if str(item or "").strip()
        }
        required_field = TASK_REQUIRED_FIELDS[task_type]
        if not task_data.get(required_field):
            _stop_without_llm(event, "任务要求为空，请返回修改。")
            return WorkbenchActionResult(handled=True)
        capability_id = f"execute.{task_type}"
        if task_type == "file" and task_data.get("operation") == "translate":
            capability_id = "execute.file.translate"
        route = route_capability_request(
            event,
            source="card",
            capability_id=capability_id,
            goal=_task_resume_text(task_type, task_data),
            confidence=1.0,
            action_force="execute",
            trusted=True,
            parameters=task_data,
        )
        if not route.allowed:
            _stop_without_llm(event, f"任务未通过路由校验：{route.reason}")
            return WorkbenchActionResult(handled=True)
        if task_type == "codex":
            authorization = authorize_codex(
                "deep_reasoning",
                authorized_by="user",
                owns_schedule=False,
            )
            if not authorization.allowed:
                _stop_without_llm(
                    event,
                    f"Codex 高级工具未获授权：{authorization.reason}",
                )
                return WorkbenchActionResult(handled=True)
            try:
                event.set_extra("codex_tool_entry", "assistant_workbench")
                event.set_extra("codex_capability", authorization.capability)
                event.set_extra("codex_authorized_by", authorization.authorized_by)
                event.set_extra("codex_role", authorization.role)
            except Exception:  # noqa: BLE001
                pass
        try:
            event.set_extra("assistant_workbench_task_type", task_type)
            event.set_extra("assistant_workbench_task_data", dict(task_data))
            source_message_id = str(
                payload.get("open_message_id")
                or getattr(getattr(event, "message_obj", None), "message_id", "")
                or uuid.uuid4().hex
            )
            platform_id = str(event.get_platform_id() or "")
            event.set_extra(
                "assistant_workbench_decision_task_id",
                f"assistant_workbench:{platform_id}:{source_message_id}",
            )
            if task_type == "copy":
                event.set_extra("disable_llm_tools", True)
        except Exception:  # noqa: BLE001
            pass
        if task_type == "file" and task_data.get("operation") == "translate":
            from dc_engines.office_translation_runtime import (
                run_translation_workbench,
            )

            event.should_call_llm(False)
            translation_result = await run_translation_workbench(
                context,
                event,
                task_data,
            )
            event.set_result(translation_result)
            event.stop_event()
            return WorkbenchActionResult(handled=True)
        return WorkbenchActionResult(
            handled=False,
            resumed_text=_task_resume_text(task_type, task_data),
        )
    elif action in {"resume_recent", "show_task"}:
        task_id = str(value.get("task_id") or "").strip()
        task_rows = await _load_task_rows(context, event)
        if not task_rows:
            _stop_without_llm(event, "当前没有可查看的真实任务。")
            return WorkbenchActionResult(handled=True)
        if not task_id:
            task_id = str(task_rows[0].get("task_id") or "")
        task_row = next(
            (row for row in task_rows if str(row.get("task_id") or "") == task_id),
            None,
        )
        if task_row is None:
            _stop_without_llm(event, "该任务不属于当前会话，请刷新任务列表。")
            return WorkbenchActionResult(handled=True)
        card_type = "assistant_task_list"
        card = build_assistant_task_detail_card(task_row)
        fallback = "任务详情已读取；本次没有调用 LLM。"
    else:
        return WorkbenchActionResult(handled=False)

    if card is None:
        return WorkbenchActionResult(handled=False)
    sent = await _send_card(
        context,
        event,
        card_type=card_type,
        card=card,
        replace_message_id=replace_message_id,
    )
    _stop_without_llm(event, "" if sent else fallback)
    return WorkbenchActionResult(handled=True)


__all__ = [
    "CODEX_TOOL_MENU_ALIASES",
    "LEGACY_TASK_MENU_PRESETS",
    "RESUME_MENU_ALIASES",
    "TASK_INTAKE_MENU_ALIASES",
    "TASK_LIST_MENU_ALIASES",
    "WORKBENCH_MENU_ALIASES",
    "WorkbenchActionResult",
    "handle_assistant_workbench_card_action",
    "try_handle_assistant_workbench",
]
