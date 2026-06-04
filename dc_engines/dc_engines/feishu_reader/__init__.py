"""W3 / 2A-3 飞书资料读取引擎。

职责：从白名单飞书文档 / 多维表格 / NAS KB 文件夹里检索资料。

v0 设计：
- ``Whitelist`` 数据模型 + yaml 加载
- ``query_resources`` 接口（v0 桩，返回白名单元信息；真 API 集成留 TODO）
- 整体框架定型，后续填 lark-oapi 真实调用

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
