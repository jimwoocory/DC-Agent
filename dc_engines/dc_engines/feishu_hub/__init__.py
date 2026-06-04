"""飞书 Hub —— 公司所有飞书相关功能的"接线员 + SIM 卡"。

理念：
- DC-Agent 里跟飞书有关的事（读文档、建群、同步通讯录、云盘→知识库、
  飞书机器人主动 @ 群、审批流、会议纪要、日历、表单 ……）**全走这里**。
- 一份 app_id/app_secret，一个 lark.Client 单例，一处限流 / 重试 / 统计。

新功能怎么用：

>>> from dc_engines.feishu_hub import get_client, call
>>> client = get_client()
>>> if client is None:
...     # 凭证缺失，业务降级
...     return
>>> from lark_oapi.api.docx.v1 import GetDocumentRequest
>>> req = GetDocumentRequest.builder().document_id(doc_id).build()
>>> resp = await call("docx.document.get", client.docx.v1.document.aget(req))

看运行时统计（dashboard / 看门狗用）：

>>> from dc_engines.feishu_hub import get_hub
>>> get_hub().stats.snapshot()
{'total_calls': 137, 'error_rate': 0.0073, ...}
"""

from .client import (
    FeishuHub,
    HubStats,
    call,
    call_sync,
    get_client,
    get_credentials,
    get_hub,
    is_enabled,
)
from .credentials import FeishuCredentials, load_credentials

__all__ = [
    "FeishuHub",
    "FeishuCredentials",
    "HubStats",
    "call",
    "call_sync",
    "get_client",
    "get_credentials",
    "get_hub",
    "is_enabled",
    "load_credentials",
]
