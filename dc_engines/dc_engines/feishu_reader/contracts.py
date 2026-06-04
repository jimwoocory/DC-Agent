"""W3 飞书资料查询数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class WhitelistDocument:
    """飞书云文档白名单条目。

    ``doc_token`` 是新版 docx 文档的 token（"doxcXXX"），URL 形如
    ``https://feishu.cn/docx/<doc_token>``。
    """

    doc_token: str
    name: str
    domain: str = "general"
    description: str = ""


@dataclass(slots=True)
class WhitelistTable:
    """飞书多维表格白名单条目。

    bitable 层级：app（一个多维表格应用）→ table（应用内某张表）→ record。
    所以一个表格需要 ``app_token`` + ``table_id`` 两个字段才能定位。
    """

    app_token: str
    table_id: str
    name: str
    domain: str = "general"
    description: str = ""
    primary_key: str | None = None


@dataclass(slots=True)
class WhitelistFolder:
    """NAS KB 文件夹白名单条目（来自 nas_sync 已挂的 4 路 KB 之一）。"""

    kb_id: str
    name: str
    domain: str = "general"
    description: str = ""


@dataclass(slots=True)
class Whitelist:
    """白名单合集。"""

    documents: list[WhitelistDocument] = field(default_factory=list)
    tables: list[WhitelistTable] = field(default_factory=list)
    folders: list[WhitelistFolder] = field(default_factory=list)

    def total(self) -> int:
        return len(self.documents) + len(self.tables) + len(self.folders)


@dataclass(slots=True)
class FeishuCredentials:
    """飞书 app 凭证（用于 docx + bitable 读取，跟 AstrBot 聊天 adapter 解耦）。"""

    app_id: str
    app_secret: str
    enable: bool = True


@dataclass(slots=True)
class DocContent:
    """飞书文档的纯文本提取结果。"""

    doc_token: str
    title: str
    plain_text: str
    block_count: int
    truncated: bool = False  # 超过最大字符数被截断


@dataclass(slots=True)
class TableRecord:
    """bitable 单条记录。"""

    record_id: str
    fields: dict


@dataclass(slots=True)
class QueryHit:
    """单条检索结果。"""

    source_type: str  # "document" / "table" / "folder"
    source_id: str  # doc_token / table_id / kb_id
    title: str
    domain: str
    summary: str = ""
    matched_field: str = ""
    matched_snippet: str = ""  # 命中位置上下文（v1 真内容检索时填）
    score: float = 0.0
    url: str | None = None
