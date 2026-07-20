"""Deliver LLM-driven Office workbench results as real files or Feishu docs."""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

from dc_engines.card_system import attach_card_delivery_files
from dc_engines.office_document_delivery import generate_office_deliverables
from lark_oapi.api.docx.v1 import (
    Block,
    CreateDocumentBlockChildrenRequest,
    CreateDocumentBlockChildrenRequestBody,
    CreateDocumentRequest,
    CreateDocumentRequestBody,
    Text,
    TextElement,
    TextRun,
)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import File, Plain
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


@register(
    "office_document_delivery_plugin",
    "dc_agent",
    "LLM 办公任务的 Word、Excel、PDF、文本和飞书云文档交付",
    "1.0.0",
)
class OfficeDocumentDeliveryPlugin(Star):
    """Package research and file-processing answers after card rendering."""

    def __init__(self, context: Context) -> None:
        """Initialize the Office delivery plugin.

        Args:
            context: Running AstrBot plugin context.
        """
        super().__init__(context)

    @filter.on_llm_response()
    async def retain_office_result(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ) -> None:
        """Retain the final Office text before the Feishu card consumes it.

        Args:
            event: Active Office workbench event.
            response: Current LLM response.

        Returns:
            None.
        """
        task_type = str(event.get_extra("assistant_workbench_task_type") or "")
        if task_type not in {"research", "file"} or response.is_chunk:
            return
        text = str(response.completion_text or "").strip()
        if text and response.role == "assistant":
            event.set_extra("office_delivery_text", text)

    async def _create_feishu_document(
        self,
        event: AstrMessageEvent,
        *,
        task_type: str,
        task_data: dict[str, Any],
        result_text: str,
    ) -> str:
        """Create one native Feishu cloud document from the LLM result.

        Args:
            event: Active Feishu event used to locate the platform client.
            task_type: Research or file-processing task type.
            task_data: Sanitized task fields.
            result_text: Final LLM result.

        Returns:
            URL of the created Feishu cloud document.

        Raises:
            RuntimeError: If the Feishu client cannot create or populate the doc.
        """
        platform_id = str(event.get_platform_id() or "")
        platform = self.context.get_platform_inst(platform_id)
        client = getattr(platform, "lark_api", None)
        if client is None:
            raise RuntimeError("飞书云文档通道不可用")
        task_name = str(
            task_data.get("research_question")
            or task_data.get("file_goal")
            or "办公任务结果"
        ).strip()
        suffix = "研究分析" if task_type == "research" else "文件处理"
        title = f"{task_name[:90]} · {suffix}"
        create_body = CreateDocumentRequestBody.builder().title(title).build()
        create_request = (
            CreateDocumentRequest.builder().request_body(create_body).build()
        )
        create_response = await client.docx.v1.document.acreate(create_request)
        document = getattr(getattr(create_response, "data", None), "document", None)
        document_id = str(getattr(document, "document_id", "") or "")
        if not create_response.success() or not document_id:
            raise RuntimeError(
                f"飞书云文档创建失败：{getattr(create_response, 'msg', '')}"
            )

        visible_lines = [
            "巅池办公工作台",
            suffix,
            "自动生成；如结果不符合预期，可补充要求后再次生成。",
        ]
        for raw_line in result_text.splitlines():
            line = re.sub(r"^(?:#{1,6}|[-*+]\s*|\d+[.)、]\s*)", "", raw_line).strip()
            line = re.sub(r"\*\*|__|`{1,3}|~~", "", line)
            if not line:
                continue
            visible_lines.extend(
                line[index : index + 1500] for index in range(0, len(line), 1500)
            )
            if len(visible_lines) >= 95:
                break
        children = []
        for content in visible_lines[:95]:
            text_run = TextRun.builder().content(content).build()
            text_element = TextElement.builder().text_run(text_run).build()
            text_body = Text.builder().elements([text_element]).build()
            children.append(Block.builder().block_type(2).text(text_body).build())
        write_body = (
            CreateDocumentBlockChildrenRequestBody.builder()
            .children(children)
            .index(-1)
            .build()
        )
        write_request = (
            CreateDocumentBlockChildrenRequest.builder()
            .document_id(document_id)
            .block_id(document_id)
            .request_body(write_body)
            .build()
        )
        write_response = await client.docx.v1.document_block_children.acreate(
            write_request
        )
        if not write_response.success():
            raise RuntimeError(
                f"飞书云文档写入失败：{getattr(write_response, 'msg', '')}"
            )
        return f"https://feishu.cn/docx/{document_id}"

    @filter.on_decorating_result(priority=5)
    async def append_office_deliverables(self, event: AstrMessageEvent) -> None:
        """Append selected files after the daily response card is finalized.

        Priority 5 intentionally runs after the priority-30 Feishu card renderer,
        which consumes plain text after sending the result card.

        Args:
            event: Active Office workbench event and outgoing result chain.

        Returns:
            None.
        """
        task_type = str(event.get_extra("assistant_workbench_task_type") or "")
        if task_type not in {"research", "file"}:
            return
        task_data = event.get_extra("assistant_workbench_task_data") or {}
        task_data = task_data if isinstance(task_data, dict) else {}
        result_text = str(event.get_extra("office_delivery_text") or "").strip()
        if not result_text:
            return
        delivery_format = str(
            (
                task_data.get("delivery_format")
                if task_type == "research"
                else task_data.get("output_format")
            )
            or ""
        ).strip()
        if not delivery_format:
            delivery_format = "result_card" if task_type == "research" else "docx"
        if delivery_format == "result_card":
            return

        result = event.get_result()
        if result is None:
            result = MessageEventResult()
            event.set_result(result)
        try:
            if delivery_format == "feishu_doc":
                url = await self._create_feishu_document(
                    event,
                    task_type=task_type,
                    task_data=task_data,
                    result_text=result_text,
                )
                result.chain.append(Plain(f"飞书云文档已生成：{url}"))
                event.set_extra("office_delivery_artifacts", {"feishu_doc": url})
                card_message_id = str(
                    event.get_extra("_daily_card_thinking_stream_id") or ""
                )
                if card_message_id:
                    attach_card_delivery_files(card_message_id, [url])
                return

            output_dir = (
                Path(get_astrbot_data_path())
                / "output"
                / "office_workbench"
                / uuid.uuid4().hex
            )
            artifacts = await asyncio.to_thread(
                generate_office_deliverables,
                task_type,
                task_data,
                result_text,
                output_dir=output_dir,
            )
            for path in artifacts.values():
                result.chain.append(File(name=path.name, file=str(path)))
            event.set_extra(
                "office_delivery_artifacts",
                {key: str(path) for key, path in artifacts.items()},
            )
            card_message_id = str(
                event.get_extra("_daily_card_thinking_stream_id") or ""
            )
            if card_message_id:
                attach_card_delivery_files(
                    card_message_id,
                    [str(path) for path in artifacts.values()],
                )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.exception("[office_delivery] artifact generation failed")
            result.chain.append(Plain(f"交付文件生成失败：{exc}"))
