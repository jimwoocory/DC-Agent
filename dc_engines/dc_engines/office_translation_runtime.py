"""AstrBot runtime integration for real Office document translation."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from lark_oapi.api.docx.v1 import (
    Block,
    CreateDocumentBlockChildrenRequest,
    CreateDocumentBlockChildrenRequestBody,
    CreateDocumentRequest,
    CreateDocumentRequestBody,
    RawContentDocumentRequest,
    Text,
    TextElement,
    TextRun,
)
from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest

from astrbot.api import logger
from astrbot.api.event import MessageEventResult
from astrbot.core.message.components import File, Plain
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from dc_engines.office_translation import (
    SUPPORTED_SOURCE_SUFFIXES,
    TranslationError,
    translate_office_document,
)

_FEISHU_URL_RE = re.compile(
    r"https?://[^\s，,；;（）()]*?(?:feishu\.cn|larksuite\.com)/"
    r"(wiki|docx|docs)/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_SOURCE_URL_RE = re.compile(r"https?://[^\s，,；;（）()]+", re.IGNORECASE)


def _get_lark_client(context: Any, event: Any) -> Any | None:
    """Resolve the Lark client bound to the current AstrBot platform.

    Args:
        context: AstrBot runtime context.
        event: Current internal workbench event.

    Returns:
        Lark API client, or None when the platform is unavailable.
    """
    platform_id = str(event.get_platform_id() or "")
    get_platform_inst = getattr(context, "get_platform_inst", None)
    if callable(get_platform_inst):
        try:
            platform = get_platform_inst(platform_id)
            client = getattr(platform, "lark_api", None)
            if client is not None:
                return client
        except Exception:  # noqa: BLE001
            pass
    platform_manager = getattr(context, "platform_manager", None)
    for platform in getattr(platform_manager, "platform_insts", None) or []:
        client = getattr(platform, "lark_api", None)
        if client is not None:
            return client
    return None


def _get_aihubmix_provider(context: Any) -> Any:
    """Find a configured AstrBot AIHubMix text provider.

    Args:
        context: AstrBot runtime context.

    Returns:
        Provider capable of Qwen-MT model overrides.

    Raises:
        TranslationError: If no AIHubMix provider is configured.
    """
    get_all_providers = getattr(context, "get_all_providers", None)
    providers = get_all_providers() if callable(get_all_providers) else []
    for provider in providers or []:
        config = getattr(provider, "provider_config", None) or {}
        provider_type = str(config.get("type") or "").lower()
        if (
            provider_type == "aihubmix_chat_completion"
            or "aihubmix" in type(provider).__name__.lower()
        ):
            return provider
    raise TranslationError(
        "provider_unavailable",
        "没有找到可用的 AIHubMix 翻译 Provider",
        "请在 AstrBot Provider Manager 配置 AIHubMix 后重新执行；专业模式不会静默降级。",
    )


async def _fetch_feishu_source(
    client: Any,
    url: str,
    destination: Path,
) -> Path:
    """Fetch one Feishu docx or wiki document as real source text.

    Args:
        client: Bound Lark OpenAPI client.
        url: Explicit or picker-derived Feishu document URL.
        destination: Runtime source directory.

    Returns:
        Local UTF-8 text path containing the cloud document body.

    Raises:
        TranslationError: If the document type, permission, or content fails.
    """
    match = _FEISHU_URL_RE.search(url)
    if not match:
        raise TranslationError(
            "unsupported_cloud_document",
            "飞书链接不是可读取的文档地址",
            "请使用飞书 docx 或 wiki 文档链接后重试。",
        )
    document_type, token = match.group(1).lower(), match.group(2)
    title = "飞书云文档"
    if document_type == "wiki":
        request = GetNodeSpaceRequest.builder().token(token).build()
        response = await client.wiki.v2.space.aget_node(request)
        node = getattr(getattr(response, "data", None), "node", None)
        if not response.success() or node is None:
            raise TranslationError(
                "cloud_document_permission",
                "飞书知识库文档读取失败",
                "请把巅池-Agent小助手加入文档协作者，并确认应用已发布 wiki 读取权限。",
            )
        if str(getattr(node, "obj_type", "") or "").lower() not in {"doc", "docx"}:
            raise TranslationError(
                "unsupported_cloud_document",
                "第一期只支持飞书文档类型的知识库节点",
                "请将内容复制到飞书文档，或导出 Word 后上传。",
            )
        token = str(getattr(node, "obj_token", "") or "")
        title = str(getattr(node, "title", "") or title)
    request = RawContentDocumentRequest.builder().document_id(token).build()
    response = await client.docx.v1.document.araw_content(request)
    body = getattr(getattr(response, "data", None), "content", None)
    content = str(body or "").strip()
    if not response.success() or not content:
        raise TranslationError(
            "cloud_document_permission",
            "飞书云文档无法读取或没有可翻译正文",
            "请把巅池-Agent小助手加入文档协作者；纯图片文档请先 OCR。",
        )
    destination.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r"[^\w\-.\u3400-\u9fff]+", "_", title).strip("._")[:80]
    source_path = destination / f"{safe_title or 'feishu_document'}.txt"
    source_path.write_text(content, encoding="utf-8")
    return source_path


async def _validate_public_host(hostname: str, port: int) -> None:
    """Reject local or special network destinations before URL download.

    Args:
        hostname: URL hostname.
        port: Resolved URL port.

    Raises:
        TranslationError: If DNS fails or any resolved address is non-public.
    """
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise TranslationError(
            "source_download_failed",
            "文件链接域名无法解析",
            "请检查链接是否可公开访问，或改为本地上传。",
        ) from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise TranslationError(
                "unsafe_source_url",
                "文件链接指向内网或保留地址",
                "请使用可公开访问的 HTTPS 文件链接，或改为本地上传。",
            )


async def _download_source(url: str, destination: Path) -> Path:
    """Download one bounded public HTTPS Office file without unsafe redirects.

    Args:
        url: Explicit HTTPS source URL.
        destination: Runtime source directory.

    Returns:
        Local downloaded source path.

    Raises:
        TranslationError: If URL safety, type, size, or transfer validation fails.
    """
    current_url = url
    destination.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as client:
        for _redirect in range(4):
            parsed = urlsplit(current_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise TranslationError(
                    "invalid_source_url",
                    "文件链接格式无效",
                    "请提供不含账号密码的完整 HTTPS 文件地址。",
                )
            await _validate_public_host(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
            )
            suffix = Path(parsed.path).suffix.lower()
            if suffix not in SUPPORTED_SOURCE_SUFFIXES:
                raise TranslationError(
                    "unsupported_format",
                    "文件链接必须明确指向支持的文件扩展名",
                    "请使用以 .docx、.xlsx、.pdf、.txt、.md 或 .markdown 结尾的链接。",
                )
            async with client.stream("GET", current_url) as response:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        break
                    current_url = urljoin(current_url, location)
                    continue
                if response.status_code != 200:
                    raise TranslationError(
                        "source_download_failed",
                        f"文件链接返回 HTTP {response.status_code}",
                        "请确认链接无需登录且未过期，或改为本地上传。",
                    )
                try:
                    declared_size = int(response.headers.get("content-length") or 0)
                except ValueError:
                    declared_size = 0
                if declared_size > 80 * 1024 * 1024:
                    raise TranslationError(
                        "source_too_large",
                        "远程文件超过 80 MB",
                        "请压缩或拆分文件后改为本地上传。",
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 80 * 1024 * 1024:
                        raise TranslationError(
                            "source_too_large",
                            "远程文件超过 80 MB",
                            "请压缩或拆分文件后改为本地上传。",
                        )
                source_path = destination / f"remote_source{suffix}"
                source_path.write_bytes(content)
                return source_path
    raise TranslationError(
        "source_download_failed",
        "文件链接重定向次数过多",
        "请提供最终文件直链，或改为本地上传。",
    )


async def _resolve_source(
    context: Any,
    event: Any,
    task_data: dict[str, Any],
    source_dir: Path,
) -> Path:
    """Resolve exactly one local, Feishu, or public URL source document.

    Args:
        context: AstrBot runtime context.
        event: Internal workbench event carrying local File components.
        task_data: Saved workbench task settings and material note.
        source_dir: Runtime source directory for fetched material.

    Returns:
        One local source path ready for the translation engine.

    Raises:
        TranslationError: If the source is absent, ambiguous, or unreadable.
    """
    components = getattr(getattr(event, "message_obj", None), "message", None) or []
    local_paths: list[Path] = []
    for component in components:
        if not isinstance(component, File):
            continue
        raw_path = str(getattr(component, "file_", "") or "")
        path = Path(raw_path)
        if path.is_file():
            local_paths.append(path)
    if len(local_paths) > 1:
        raise TranslationError(
            "multiple_sources",
            "第一期全文件翻译每次只处理一份原文件",
            "请返回工作台只保留一个原文件后重新执行。",
        )
    if local_paths:
        return local_paths[0]

    source_note = str(task_data.get("file_source") or "")
    urls = _SOURCE_URL_RE.findall(source_note)
    if len(urls) != 1:
        raise TranslationError(
            "source_missing",
            "没有找到唯一可读取的原文件",
            "请重新上传一份文件、选择一份飞书云文档或填写一个明确文件直链。",
        )
    feishu_match = _FEISHU_URL_RE.search(urls[0])
    if feishu_match:
        client = _get_lark_client(context, event)
        if client is None:
            raise TranslationError(
                "cloud_channel_unavailable",
                "飞书云文档通道不可用",
                "请稍后重试，或将文档导出为 Word 后本地上传。",
            )
        return await _fetch_feishu_source(client, urls[0], source_dir)
    return await _download_source(urls[0], source_dir)


async def _create_feishu_document(
    context: Any,
    event: Any,
    *,
    title: str,
    content: str,
) -> str:
    """Create and populate one native Feishu cloud document.

    Args:
        context: AstrBot runtime context.
        event: Current internal workbench event.
        title: New cloud document title.
        content: Completed translated body.

    Returns:
        New Feishu document URL.

    Raises:
        TranslationError: If creation or any block write fails.
    """
    client = _get_lark_client(context, event)
    if client is None:
        raise TranslationError(
            "cloud_channel_unavailable",
            "飞书云文档交付通道不可用",
            "请改选 Word、PDF 或文本文件后重新生成。",
        )
    create_body = CreateDocumentRequestBody.builder().title(title[:120]).build()
    create_request = CreateDocumentRequest.builder().request_body(create_body).build()
    create_response = await client.docx.v1.document.acreate(create_request)
    document = getattr(getattr(create_response, "data", None), "document", None)
    document_id = str(getattr(document, "document_id", "") or "")
    if not create_response.success() or not document_id:
        raise TranslationError(
            "cloud_delivery_failed",
            "飞书云文档创建失败",
            "请检查应用 docx 写入权限，或改选本地文件交付。",
        )
    paragraphs: list[str] = []
    for raw_line in content.splitlines() or [content]:
        line = raw_line.strip()
        if not line:
            continue
        paragraphs.extend(
            line[index : index + 1500] for index in range(0, len(line), 1500)
        )
    for start in range(0, len(paragraphs), 50):
        children = []
        for paragraph in paragraphs[start : start + 50]:
            text_run = TextRun.builder().content(paragraph).build()
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
            raise TranslationError(
                "cloud_delivery_failed",
                "飞书云文档已创建但正文写入失败",
                "任务记录已保留；请检查 docx 写入权限后重新生成。",
            )
    return f"https://feishu.cn/docx/{document_id}"


async def run_translation_workbench(
    context: Any,
    event: Any,
    task_data: dict[str, Any],
) -> MessageEventResult:
    """Execute one workbench translation through AstrBot and return delivery.

    Args:
        context: AstrBot runtime context with Provider Manager and Lark platform.
        event: Trusted internal workbench event carrying source attachments.
        task_data: Sanitized translation task settings.

    Returns:
        Stopped result containing real files, native cloud URL, or recovery error.
    """
    run_id = uuid.uuid4().hex
    output_dir = (
        Path(get_astrbot_data_path()) / "output" / "office_translations" / run_id
    )
    source_dir = output_dir / "source"
    result = MessageEventResult(chain=[]).use_t2i(False).stop_event()
    trace_manifest_path = output_dir / "manifest.json"
    try:
        provider = _get_aihubmix_provider(context)
        source_path = await _resolve_source(
            context,
            event,
            task_data,
            source_dir,
        )
        traced_task_data = dict(task_data)
        traced_task_data["workspace_token"] = str(
            event.get_extra("assistant_workbench_workspace_token") or ""
        )
        translation = await translate_office_document(
            provider,
            source_path,
            traced_task_data,
            output_dir=output_dir,
            run_id=run_id,
        )
        trace_manifest_path = translation.manifest_path
        artifacts: dict[str, str] = {
            key: str(path) for key, path in translation.artifacts.items()
        }
        if str(task_data.get("output_format") or "") == "feishu_doc":
            title = (
                f"{source_path.stem} · {task_data.get('target_language', '译文')}译文"
            )
            cloud_url = await _create_feishu_document(
                context,
                event,
                title=title,
                content=translation.delivery_text,
            )
            artifacts["feishu_doc"] = cloud_url
            manifest = json.loads(translation.manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["feishu_doc"] = cloud_url
            translation.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            result.chain.append(Plain(f"翻译完成，飞书云文档已生成：{cloud_url}"))
        else:
            result.chain.append(
                Plain(
                    "翻译完成"
                    f"｜目标语言：{task_data.get('target_language', '')}"
                    f"｜模型：{translation.model}"
                )
            )
            for path in translation.artifacts.values():
                result.chain.append(File(name=path.name, file=str(path)))
        event.set_extra("office_translation_run_id", run_id)
        event.set_extra("office_translation_manifest", str(translation.manifest_path))
        event.set_extra("office_translation_artifacts", artifacts)
        logger.info(
            "[office_translation] completed run_id=%s model=%s source=%s",
            run_id,
            translation.model,
            source_path.name,
        )
    except TranslationError as exc:
        if trace_manifest_path.is_file():
            try:
                failed_manifest = json.loads(
                    trace_manifest_path.read_text(encoding="utf-8")
                )
                if failed_manifest.get(
                    "status"
                ) == "completed" and not failed_manifest.get("artifacts"):
                    failed_manifest["status"] = "failed"
                    failed_manifest["error"] = {
                        "code": exc.code,
                        "message": str(exc),
                        "recovery": exc.recovery,
                        "locations": exc.locations,
                    }
                    trace_manifest_path.write_text(
                        json.dumps(failed_manifest, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                event.set_extra("office_translation_manifest", str(trace_manifest_path))
            except (OSError, TypeError, json.JSONDecodeError):
                pass
        locations = f" 失败位置：{'、'.join(exc.locations)}。" if exc.locations else ""
        result.chain.append(
            Plain(f"翻译任务未生成交付文件：{exc}。{locations}恢复方式：{exc.recovery}")
        )
        event.set_extra(
            "office_translation_error",
            {
                "run_id": run_id,
                "code": exc.code,
                "message": str(exc),
                "recovery": exc.recovery,
                "locations": exc.locations,
            },
        )
        logger.warning(
            "[office_translation] failed run_id=%s code=%s",
            run_id,
            exc.code,
        )
    except Exception as exc:  # noqa: BLE001
        result.chain.append(
            Plain(
                "翻译任务未生成交付文件：执行链路异常。"
                "恢复方式：请保留原文件并重新执行；若重复失败请检查运行日志。"
            )
        )
        event.set_extra(
            "office_translation_error",
            {"run_id": run_id, "code": "runtime_failure", "message": str(exc)},
        )
        logger.exception("[office_translation] runtime failed run_id=%s", run_id)
    return result


__all__ = ["run_translation_workbench"]
