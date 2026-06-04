# Hermes Bridge 集成说明

## 概述

`hermes_bridge` 插件是 AstrBot 与 Hermes Agent 之间的双向桥接层：
- 将 QQ / 飞书消息转发给 Hermes Webhook
- 接收 Hermes 的响应并发回原平台用户

## 核心文件

| 文件 | 职责 |
|------|------|
| `data/plugins/hermes_bridge/hermes_bridge.py` | 主插件：消息转发、响应回传、Webhook 服务器 |
| `data/plugins/hermes_bridge/router.py` | SessionRouter：平台用户 ↔ session_key 持久化映射 |

## 数据库

```
data/hermes_sessions.db   # SessionRouter：platform_user → session_key 映射
```

## 消息转发流程（AstrBot → Hermes）

```
员工发消息（QQ/飞书）
       ↓
on_message(event)
       ↓
get_or_create_session(user_id, platform_type)
缓存 session_key → unified_msg_origin
       ↓
_send_to_hermes(message_data)
POST http://localhost:8644/webhooks/astrbot_qq
包含: user_id, session_key, unified_msg_origin, message, platform...
```

## 响应回传流程（Hermes → 员工）

```
Hermes 处理完成，POST http://localhost:8645/hermes_response
       ↓
_handle_hermes_response(request)
       ↓
解析 unified_msg_origin：
  1. 优先从响应体取 unified_msg_origin
  2. Fallback: 从内存缓存 _umo_cache[session_key] 取
  3. 都没有 → 404，响应丢失
       ↓
_send_to_platform(umo, response_text)
context.send_message(umo, MessageChain([Plain(text)]))
```

## 平台类型映射

`PlatformType.from_astrbot_platform_id()` 负责将 AstrBot platform_id 转换为 SessionRouter 的枚举：

| AstrBot platform_id | PlatformType |
|---------------------|--------------|
| `qq_official` | `QQ` |
| `lark` | `FEISHU` |
| `webchat` | `WEBUI` |
| 其他 | 尝试 from_string，失败默认 QQ |

## SessionRouter

- 基于 SQLite 持久化，重启不丢失会话映射
- `get_or_create_session(PlatformUser)` → 返回 session_key（UUID）
- `get_platform_user_by_session(session_key)` → 反查 PlatformUser
- `_umo_cache` 是内存缓存，重启后由首条消息重建

## 配置

在 AstrBot 配置中添加（`hermes-config/config.yaml` 对应部分）：

```json
{
  "hermes_bridge": {
    "webhook_url": "http://localhost:8644/webhooks/astrbot_qq",
    "secret": "astrbot-secret-key",
    "response_port": 8645
  }
}
```

## HMAC 签名

每条发往 Hermes 的请求都带有 `X-Hub-Signature-256` Header（SHA256 HMAC），Hermes 侧需要用相同 secret 验证。

## 已知注意事项

**umo 缓存重启失效**：`_umo_cache` 是内存字典，AstrBot 重启后清空。如果 Hermes 在重启后立即推送响应（没有新的入站消息重建缓存），`unified_msg_origin` 解析失败，响应丢失。日志会打印 WARNING 提示。

**Hermes 回传需携带 unified_msg_origin**：Hermes 侧在响应体中应透传 `unified_msg_origin` 字段（AstrBot 入站时已包含在 payload 中），这样不依赖内存缓存也能正确回传。

**飞书 DNS 偶发失败**：Surge TUN 初始化期间（AstrBot 启动后约 30 秒内）飞书 API 域名解析可能失败，已在 `start-astrbot.sh` 中加入 30 秒启动延迟规避。
