# feishu_hub — 公司飞书"接线员"

> 公司所有飞书相关功能的统一入口。一份钥匙、一个客户端、一处限流/重试/统计。

## 一句话

```
任何想跟飞书 API 打交道的代码 → 都从这里走，不要自己 build lark.Client。
```

## 它管什么

| 管 | 不管 |
|---|---|
| 凭证（app_id / app_secret 集中加载） | 业务逻辑（建群叫啥名、发啥消息）|
| `lark.Client` 单例（token cache 共享） | 业务规则（什么人能用什么功能）|
| 调用统计（用于 watchdog + dashboard）| 业务数据 |

## 给上层用的 API

```python
from dc_engines.feishu_hub import get_client, is_enabled, call

# 检查飞书是否可用（凭证齐？已 build？）
if not is_enabled():
    return  # 业务降级

# 拿单例客户端
client = get_client()

# 用 call() 包装调用，自动记到 stats
from lark_oapi.api.docx.v1 import GetDocumentRequest
req = GetDocumentRequest.builder().document_id("docxXXX").build()
resp = await call("docx.document.get", client.docx.v1.document.aget(req))
```

## 凭证从哪读

按优先级：

1. `data/feishu_whitelist.yaml`（**主源**，ops 配的地方）
2. `nas_sync/config.yaml`（兼容老 feishu_sync.py，迁移过渡期用）

格式：

```yaml
feishu:
  app_id: cli_a1b2c3...
  app_secret: ABCdef...
  enable: true   # false 表示业务关闭飞书
```

## 凭证缺失时

`get_client()` 返 `None`，`is_enabled()` 返 `False`，所有上层 plugin/engine
**统一走 disabled 分支**——不抛异常、不刷错误日志，业务自己决定降级方式。

## 运行时统计

```python
from dc_engines.feishu_hub import get_hub
get_hub().stats.snapshot()
# {
#   'total_calls': 1234,
#   'total_errors': 12,
#   'error_rate': 0.0097,
#   'calls_by_method': {'docx.document.get': 832, 'contact.user.list': 402},
#   'errors_by_method': {'docx.document.get': 7, 'contact.user.list': 5},
#   'last_error': 'TimeoutError: ...',
#   ...
# }
```

dc-watchdog 会定时拉这个快照，error_rate 高 / 某个 method 持续失败 → 告警。

## 设计取舍

- **单例 vs 多实例**：单例。lark-oapi 内部有 tenant_access_token 缓存，多
  实例会导致 token 频繁刷新 + 限流配额加倍消耗。
- **自动 record_call vs 手动**：当前手动（业务 `await call("...", coro)`）。
  好处：method 名字业务自己取，比 introspect 调用栈准。坏处：忘了 call()
  就漏统计。
- **不在 hub 做业务级 API**：read_doc / list_users / create_chat 这些业务
  方法继续在 `feishu_reader / feishu_writer / employee_directory.sync` 里。
  hub 只管"用同一个 client 调飞书"这一层。

## 迁移路线

| 阶段 | 改动 |
|---|---|
| 1（当下）| hub 上线，不动现有 4 处代码——它们继续各自 build client |
| 2（测试通过后）| `feishu_reader / writer` 内部改成 `get_client()`，public API 不变 |
| 3 | 重写 `nas_sync/feishu_sync.py` 用 lark-oapi 替代 raw HTTP，走 hub |
| 4（远期）| 删 `nas_sync/config.yaml` 的 feishu 段，凭证 single source |
