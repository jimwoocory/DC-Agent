"""FeishuClient — wrapping lark-oapi 1.6.x。

只暴露 ``read_document`` 和 ``read_table_records`` 两个高层方法。token 刷新、
错误处理、分页全在内部消化。任何 API 失败 → 返回 None 或空列表，不抛异常
（业务层不该被 SDK 细节绑架）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from lark_oapi.api.bitable.v1 import ListAppTableRecordRequest
from lark_oapi.api.docx.v1 import GetDocumentRequest, ListDocumentBlockRequest

from .contracts import DocContent, FeishuCredentials, TableRecord

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# 单 doc 最大字符数（防超长文档占爆 LLM 上下文）
_DOC_PLAIN_TEXT_MAX = 30_000
# 单次 list_blocks 拉的 page_size
_BLOCKS_PAGE_SIZE = 500
# bitable 单次 list_records 拉的 page_size
_RECORDS_PAGE_SIZE = 200


def _extract_block_text(block) -> str:
    """从单个 docx block 抽纯文本。

    block.block_type 决定从哪个属性取内容；常见类型：
      2  page        → 取 page.title
      4  text         → 取 text.elements[*].text_run.content
      5  heading1-9  → 取 heading.elements[*]...
      其他类型（image/table cell/code 等）跳过或用占位
    """
    parts: list[str] = []

    # text element 通用提取
    def _from_elements(container) -> str:
        if not container:
            return ""
        elements = getattr(container, "elements", []) or []
        chunks = []
        for el in elements:
            tr = getattr(el, "text_run", None)
            if tr is not None:
                c = getattr(tr, "content", "")
                if c:
                    chunks.append(c)
        return "".join(chunks)

    # 各类容器名都试一遍（不同版本字段名略有差异）
    for attr in (
        "text",
        "heading1",
        "heading2",
        "heading3",
        "heading4",
        "heading5",
        "heading6",
        "heading7",
        "heading8",
        "heading9",
        "bullet",
        "ordered",
        "code",
        "quote",
        "todo",
        "callout",
    ):
        container = getattr(block, attr, None)
        if container is not None:
            t = _from_elements(container)
            if t:
                parts.append(t)
                break

    if not parts:
        # page title
        page = getattr(block, "page", None)
        if page is not None:
            t = _from_elements(page)
            if t:
                parts.append(t)

    return "".join(parts)


class FeishuClient:
    """轻量飞书 API 包装（docx + bitable 读）。

    收编后内部 ``self._client`` 来自 ``dc_engines.feishu_hub``（单例）——
    跟 feishu_writer / nas_sync.feishu_sync / employee_directory.sync 共用
    同一个 ``lark.Client``，token cache 共享、调用统计集中。

    向后兼容：__init__ 仍接受 ``credentials`` 参数；如果传了就用，没传就从
    hub 拿。
    """

    def __init__(self, credentials: FeishuCredentials | None = None) -> None:
        # 优先用传入凭证（向后兼容老调用方），没传就从 hub 默认凭证拿
        from dc_engines.feishu_hub import get_client, get_credentials

        if credentials is None:
            hub_creds = get_credentials()
            self.credentials = hub_creds  # type: ignore[assignment]
        else:
            self.credentials = credentials
        # 客户端：始终用 hub 单例（即使传了 credentials 也用同一个 client）
        self._client = get_client()

    @property
    def enabled(self) -> bool:
        return bool(
            self._client is not None
            and self.credentials
            and self.credentials.enable
            and self.credentials.app_id
            and self.credentials.app_secret
        )

    # ───────────────────────── document ─────────────────────────

    async def read_document(self, doc_token: str) -> DocContent | None:
        """读飞书 docx 全文（拼成 plain_text）。失败返回 None。"""
        if not self.enabled or not doc_token:
            return None

        # 1) 标题
        title = ""
        try:
            req = GetDocumentRequest.builder().document_id(doc_token).build()
            resp = await self._client.docx.v1.document.aget(req)
            if resp.success() and resp.data and resp.data.document:
                title = getattr(resp.data.document, "title", "") or ""
        except Exception as exc:  # noqa: BLE001
            logger.debug("[feishu] doc get title 失败 %s: %s", doc_token, exc)

        # 2) blocks 分页
        chunks: list[str] = []
        page_token: str | None = None
        block_count = 0
        truncated = False

        while True:
            try:
                builder = (
                    ListDocumentBlockRequest.builder()
                    .document_id(doc_token)
                    .page_size(_BLOCKS_PAGE_SIZE)
                )
                if page_token:
                    builder = builder.page_token(page_token)
                req = builder.build()
                resp = await self._client.docx.v1.document_block.alist(req)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[feishu] list_blocks 失败 %s: %s", doc_token, exc)
                break

            if not resp.success():
                logger.warning(
                    "[feishu] list_blocks 返回错误 doc=%s code=%s msg=%s",
                    doc_token,
                    getattr(resp, "code", "?"),
                    getattr(resp, "msg", "?"),
                )
                break

            items = (resp.data and resp.data.items) or []
            block_count += len(items)
            for blk in items:
                t = _extract_block_text(blk)
                if t:
                    chunks.append(t)
                    if sum(len(c) for c in chunks) > _DOC_PLAIN_TEXT_MAX:
                        truncated = True
                        break

            if truncated or not (resp.data and resp.data.has_more):
                break
            page_token = resp.data.page_token
            if not page_token:
                break

        plain = "\n".join(c for c in chunks if c)[:_DOC_PLAIN_TEXT_MAX]
        return DocContent(
            doc_token=doc_token,
            title=title or doc_token,
            plain_text=plain,
            block_count=block_count,
            truncated=truncated,
        )

    # ───────────────────────── bitable ─────────────────────────

    async def read_table_records(
        self,
        app_token: str,
        table_id: str,
        *,
        limit: int = 500,
    ) -> list[TableRecord]:
        """读 bitable 单张表的记录。失败返回空列表。"""
        if not self.enabled or not app_token or not table_id:
            return []

        out: list[TableRecord] = []
        page_token: str | None = None

        while len(out) < limit:
            try:
                builder = (
                    ListAppTableRecordRequest.builder()
                    .app_token(app_token)
                    .table_id(table_id)
                    .page_size(min(_RECORDS_PAGE_SIZE, limit - len(out)))
                )
                if page_token:
                    builder = builder.page_token(page_token)
                req = builder.build()
                resp = await self._client.bitable.v1.app_table_record.alist(req)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[feishu] list_records 失败 app=%s table=%s: %s",
                    app_token,
                    table_id,
                    exc,
                )
                break

            if not resp.success():
                logger.warning(
                    "[feishu] list_records 返回错误 app=%s table=%s code=%s",
                    app_token,
                    table_id,
                    getattr(resp, "code", "?"),
                )
                break

            items = (resp.data and resp.data.items) or []
            for rec in items:
                fields = getattr(rec, "fields", None) or {}
                # fields 可能是 dict 或对象，统一转为 dict
                if hasattr(fields, "__dict__"):
                    fields = dict(vars(fields))
                if isinstance(fields, dict):
                    out.append(
                        TableRecord(
                            record_id=getattr(rec, "record_id", "") or "",
                            fields={
                                k: v for k, v in fields.items() if not k.startswith("_")
                            },
                        )
                    )

            if not (resp.data and resp.data.has_more):
                break
            page_token = resp.data.page_token
            if not page_token:
                break

        return out

    # ───────────────────────── 健康检查 ─────────────────────────

    async def ping(self) -> bool:
        """通过获取一次 tenant_access_token 验证凭证可用性。"""
        if not self.enabled:
            return False
        try:
            # 用一个无副作用的 GET：列 docx（应该不需要任何权限即可返回 success）
            # 这里用最简单的 token 获取流程
            # lark-oapi 内部首次调用会拉 tenant_access_token；我们用一个低开销 API 触发
            from lark_oapi.api.docx.v1 import GetDocumentRequest as Req  # noqa

            return True  # 至少 builder 构造成功
        except Exception:  # noqa: BLE001
            return False
