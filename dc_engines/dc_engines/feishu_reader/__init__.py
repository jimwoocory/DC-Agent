"""W3 / 2A-3 飞书资料读取引擎。

职责：从白名单飞书文档 / 多维表格 / NAS KB 文件夹里检索资料。

版本：
- ``Whitelist`` 数据模型 + yaml 加载
- ``query_resources_v0`` 返回带 provenance 的白名单元信息命中
- ``query_resources_v1`` 在凭证可用时读取 docx / bitable 内容，失败源安全退回元信息

设计上 LLM 推理由调用方做（Star plugin），本引擎只负责"按 keyword 找到候选源"。
"""

from .client import FeishuClient
from .contracts import (
    DocContent,
    FeishuCredentials,
    QueryHit,
    TableRecord,
    Whitelist,
    WhitelistDocument,
    WhitelistFolder,
    WhitelistTable,
)
from .query_engine import (
    query_resources,
    query_resources_v0,
    query_resources_v1,
)
from .whitelist import load_whitelist

__all__ = [
    "DocContent",
    "FeishuClient",
    "FeishuCredentials",
    "QueryHit",
    "TableRecord",
    "Whitelist",
    "WhitelistDocument",
    "WhitelistFolder",
    "WhitelistTable",
    "load_whitelist",
    "query_resources",
    "query_resources_v0",
    "query_resources_v1",
]
