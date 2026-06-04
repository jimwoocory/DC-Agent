"""飞书 wiki / docx / doc URL 直读 LLM 工具。

通过 lark_oapi 调飞书开放平台 API，机器人身份（tenant_access_token）读。
- wiki URL → 调 wiki.v2.space.aget_node 拿 obj_token + obj_type → 走对应读法
- docx URL → 调 docx.v1.document.araw_content 直读纯文本

返回结构化字符串给 LLM：标题 + 来源 + 内容（截断到 MAX_CHARS）。

⚠️ 失败时如实返回错误（不让 LLM 编"由于技术限制无法访问"）：
- 401/403 → "机器人没该文档的访问权限，请把机器人加进协作者"
- node_token 解不出 → "URL 格式不支持，请检查"
- 其他飞书 API 错 → "飞书 API 错误: {msg}"
"""

from __future__ import annotations

import re
from pathlib import Path

import lark_oapi as lark
import yaml
from lark_oapi.api.docx.v1 import RawContentDocumentRequest
from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderRequest

# 配置（跟 feishu_resource_plugin 共用一份）
FEISHU_CONFIG_PATH = Path("/Users/dianchi/DC-Agent/data/feishu_whitelist.yaml")

# 内容截断（避免单次 fetch 撑爆 LLM context）
MAX_CHARS = 8000

# URL 模式（识别飞书各类文档链接）
_URL_RE = re.compile(
    r"https?://[^\s]*?feishu\.cn/(wiki|docx|docs)/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)


def _load_feishu_creds() -> tuple[str, str] | None:
    """从 feishu_whitelist.yaml 读 app_id / app_secret。"""
    try:
        if not FEISHU_CONFIG_PATH.exists():
            return None
        data = yaml.safe_load(FEISHU_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        feishu = data.get("feishu", {}) if isinstance(data, dict) else {}
        if not isinstance(feishu, dict):
            return None
        if not feishu.get("enable", True):
            return None
        app_id = feishu.get("app_id", "").strip()
        app_secret = feishu.get("app_secret", "").strip()
        if not app_id or not app_secret:
            return None
        return app_id, app_secret
    except Exception as exc:
        logger.warning("[feishu_doc_fetcher] 读凭证失败：%s", exc)
        return None


def _parse_feishu_url(url: str) -> tuple[str, str] | None:
    """解析飞书 URL，返回 (doc_type, token)。"""
    if not url:
        return None
    m = _URL_RE.search(url)
    if not m:
        return None
    return m.group(1).lower(), m.group(2)


def _truncate(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... [内容截断 · 共 {len(text)} 字符，仅显示前 {limit}]"


@register(
    "feishu_doc_fetcher",
    "dc_agent",
    "飞书 Wiki/Docx 直读工具（给 LLM 用）",
    "1.0.0",
)
class FeishuDocFetcherPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._client: lark.Client | None = None
        self._init_client()

    def _init_client(self) -> None:
        creds = _load_feishu_creds()
        if not creds:
            logger.warning(
                "[feishu_doc_fetcher] 未配 feishu.app_id / app_secret，"
                "fetch_feishu_doc 工具会回错误"
            )
            return
        app_id, app_secret = creds
        try:
            self._client = (
                lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
            )
            logger.info("[feishu_doc_fetcher] lark_oapi 已就绪 app_id=%s", app_id[:10])
        except Exception as exc:
            logger.warning("[feishu_doc_fetcher] lark client 初始化失败：%s", exc)

    async def _fetch_docx_raw(self, doc_token: str) -> tuple[bool, str, str]:
        """读 docx 纯文本。返 (success, title, content_or_err)."""
        assert self._client is not None
        try:
            req = RawContentDocumentRequest.builder().document_id(doc_token).build()
            resp = await self._client.docx.v1.document.araw_content(req)
        except Exception as exc:
            return False, "", f"调 docx API 异常：{exc}"
        if not resp.success():
            return (
                False,
                "",
                (
                    f"飞书 docx API 错误 code={resp.code} msg={resp.msg}。"
                    "常见原因：①机器人没文档权限（请把『巅池-Agent小助手』"
                    "加为文档协作者）；②文档已删除；③权限范围未发布"
                ),
            )
        if not resp.data:
            return False, "", "docx 返回空"
        content = (resp.data.content or "").strip()
        if not content:
            return False, "", "docx 内容为空（可能纯图片/表格文档，暂不支持解析）"
        title = content.split("\n", 1)[0][:80]  # 第一行当标题
        return True, title, content

    async def _fetch_wiki(self, node_token: str) -> tuple[bool, str, str]:
        """读 wiki node：先 get_node 拿 obj_token，再按 obj_type 读。"""
        assert self._client is not None
        try:
            req = GetNodeSpaceRequest.builder().token(node_token).build()
            resp = await self._client.wiki.v2.space.aget_node(req)
        except Exception as exc:
            return False, "", f"调 wiki API 异常：{exc}"
        if not resp.success():
            return (
                False,
                "",
                (
                    f"飞书 wiki API 错误 code={resp.code} msg={resp.msg}。"
                    "常见原因：①机器人没 wiki 权限；"
                    "②请确认应用已勾 wiki:wiki:readonly 并发布新版本"
                ),
            )
        if not resp.data or not resp.data.node:
            return False, "", "wiki node 数据为空"
        node = resp.data.node
        obj_token = node.obj_token or ""
        obj_type = (node.obj_type or "").lower()
        wiki_title = node.title or ""

        if obj_type in ("docx", "doc"):
            ok, _t, body = await self._fetch_docx_raw(obj_token)
            if ok:
                return True, wiki_title or _t, body
            return False, wiki_title, body
        return (
            False,
            wiki_title,
            (f"wiki 节点类型 {obj_type!r} 暂不支持解析（目前只支持 docx）"),
        )

    # ─── 强约束：用户消息里出现飞书 URL 时，自动注入 system_prompt 强制 LLM 调工具 ───

    @filter.on_llm_request()
    async def force_fetch_when_feishu_url(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """LLM 调用前检测：用户消息含飞书 URL → 强制注入"必须调 fetch_feishu_doc"。"""
        msg = event.message_str or ""
        urls = _URL_RE.findall(msg)
        if not urls:
            return

        url_list = []
        for doc_type, token in urls:
            url_list.append(f"  - type={doc_type} token={token}")

        injection = (
            "\n\n## ⚠️ 必须调用 fetch_feishu_doc 工具（铁律）\n"
            "用户消息里包含飞书文档链接：\n" + "\n".join(url_list) + "\n\n"
            "**禁止行为**：\n"
            "- 禁止说『无法访问飞书内部链接』『需要登录权限』『技术限制』等借口。\n"
            "- 禁止假装『已经读过 PDF / 已经分析过 / 已经起草过』。\n"
            "- 禁止让用户『手动复制内容贴过来』。\n\n"
            "**必做**：\n"
            "- 必须调用 `fetch_feishu_doc(url=...)` 工具读真实内容。\n"
            "- 工具失败时，告知用户具体错误（如『机器人没该文档权限，请把"
            "巅池-Agent小助手加为协作者』），不要编。\n"
        )
        req.system_prompt = (req.system_prompt or "") + injection
        logger.info(
            "[feishu_doc_fetcher] 检测到飞书 URL × %d，已强制注入工具约束",
            len(urls),
        )

    @filter.llm_tool(name="fetch_feishu_doc")
    async def tool_fetch_feishu_doc(
        self,
        event: AstrMessageEvent,
        url: str,
    ) -> str:
        """读飞书 wiki / 云文档真实内容（必须用此工具，禁止说『无法访问飞书链接』）。

        触发条件：用户消息里出现任何 feishu.cn/wiki/、feishu.cn/docx/、
        feishu.cn/docs/ 开头的链接时，**必须**调用本工具读内容，禁止用
        『技术限制』『需要登录』『手动复制』之类的借口拒绝。

        Args:
            url(string): 飞书文档完整 URL，支持 wiki / docx / 旧版 doc
        """
        if not url or "feishu.cn" not in url:
            return "❌ 不是合法飞书 URL（应包含 feishu.cn）"

        parsed = _parse_feishu_url(url)
        if not parsed:
            return (
                "❌ 飞书 URL 格式无法识别。"
                "支持: feishu.cn/wiki/{token} / feishu.cn/docx/{token}"
            )
        doc_type, token = parsed

        if self._client is None:
            return "❌ 飞书 client 未配置（缺 app_id/app_secret），无法读文档"

        if doc_type == "wiki":
            ok, title, body = await self._fetch_wiki(token)
        elif doc_type in ("docx", "docs"):
            ok, title, body = await self._fetch_docx_raw(token)
        else:
            return f"❌ 不支持的飞书文档类型: {doc_type}"

        if not ok:
            logger.warning(
                "[feishu_doc_fetcher] 读 %s 失败 url=%s err=%s",
                doc_type,
                url[:80],
                body[:100],
            )
            return f"❌ 飞书文档读取失败\n{body}"

        body_truncated = _truncate(body)
        logger.info(
            "[feishu_doc_fetcher] 读取成功 type=%s title=%r len=%d",
            doc_type,
            title[:40],
            len(body),
        )
        return (
            f"✅ 飞书文档内容（type={doc_type}）\n"
            f"标题：{title}\n"
            f"来源：{url}\n"
            f"─" * 30 + "\n"
            f"{body_truncated}"
        )
