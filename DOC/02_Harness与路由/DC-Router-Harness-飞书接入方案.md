# DC Router + Harness 飞书接入方案

> 版本：v0.1  
> 日期：2026-05-19  
> 状态：设计稿，下一步进入 dry-run 接入  
> 原则：新 DC Router 成功后接管唯一入口，旧 `llm_router` 关闭最终决策权。

## 1. 目标

本方案用于把 DC-Agent 的模型路由、稀缺模型排队、Harness 深任务缰绳和飞书机器人入口统一起来。

核心目标：

- 普通业务消息快速响应。
- 高价值创意 / 洞察任务进入 Gemini OAuth 稀缺队列。
- 深度创意 / 洞察任务进入 Harness + Hermes + Claude OAuth。
- 截图 / 图片 / 文件先走 `aihubmix/gemini-3-flash-preview` 做预处理。
- 飞书两个主要机器人职责隔离，避免业务 router 和运维 router 混用。
- 新路径稳定后关闭旧 router，避免两个 router 同时抢 provider。

## 2. 飞书机器人职责

### 2.1 巅池-Agent小助手

`巅池-Agent小助手` 是员工业务入口。

负责：

- 员工日常问答。
- 办公文书。
- 截图、图片、文件等多模态理解。
- 实时信息和轻量搜索类问题。
- 营销文案、slogan、脚本。
- 品牌战略、用户洞察。
- 深度创意 / 深度洞察任务的提交和回传。

它可以进入：

```text
DC Business Router
  -> provider
  -> quota gate
  -> Harness
  -> Hermes
```

### 2.2 巅池-技术（DevOps）

`巅池-技术（DevOps）` 是运维和技术入口，不是业务入口。

负责：

- 后台运行数据监控。
- router / quota gate / Harness / Hermes 状态查看。
- 队列长度、冷却时间、429、失败任务告警。
- AstrBot、飞书、Hermes、Dashboard 链路排障。
- 代码问题、报错解释、简单修复建议。
- 运维诊断、服务状态、日志分析。

它不负责：

- 营销 creative。
- 品牌 insight。
- 业务深度分析。
- 业务 Hermes 深任务。
- 使用 Gemini OAuth / Claude OAuth 处理业务内容。

它应进入：

```text
DC Ops Router
  -> system_status / queue_status / error_debug / code_ops
  -> codex/gpt-5.4 或运维工具
```

## 3. 总体链路

```text
飞书消息
  -> 按 platform_id 区分机器人
  -> 巅池-Agent小助手：Business Router
  -> 巅池-技术（DevOps）：Ops Router
  -> 其他平台：默认不接管
```

业务入口：

```text
on_message
  -> 前缀强规则
  -> 多模态预处理
  -> 强关键词规则
  -> 必要时 Gemini 3.1 Pro 裁判
  -> provider / quota gate / Harness / Hermes
```

运维入口：

```text
on_message
  -> 运维关键词 / 命令
  -> 状态查询 / 日志诊断 / 代码问题
  -> codex/gpt-5.4 或本地诊断工具
```

## 4. Business Router 分类

| intent | provider / path | 是否排队 | 是否 Harness | 说明 |
|---|---|---:|---:|---|
| `casual` | `aihubmix/gemini-3-flash-preview` | 否 | 否 | 接客、普通闲聊 |
| `ops_writing` | `aihubmix/gemini-3-flash-preview` | 否 | 否 | 通知、公告、日报、邮件 |
| `multimodal` | `aihubmix/gemini-3-flash-preview` | 否 | 否 | 截图、图片、语音、文件预处理 |
| `realtime` | `aihubmix/gemini-3-flash-preview` | 否 | 否 | 热点、时效、轻量搜索 |
| `public_opinion` | `aihubmix/grok-4.3` | 否 | 否 | 舆情、危机公关、热点攻防 |
| `simple_code` | `codex/gpt-5.4` | 否 | 否 | 简单代码、报错解释、小脚本 |
| `creative` | `gemini/3.1-pro-xhigh` | 是 | 否 | 营销文案、slogan、脚本 |
| `insight` | `gemini/3.1-pro-xhigh` | 是 | 否 | 品牌战略、用户洞察 |
| `deep_creative` | Harness + Hermes + Claude Sonnet/Opus | 是 | 是 | 深度营销方案 |
| `deep_insight` | Harness + Hermes + Claude Sonnet/Opus | 是 | 是 | 深度战略 / 洞察报告 |
| `fallback` | `codex/gpt-5.5-high` | 否 | 否 | router 未命中兜底 |

## 5. Business Router 决策流程

```text
on_message:

1. 前缀指令优先
   #深度 / #洞察 / #创意 / #PRD / #舆情 / #代码
   -> 直接锁定大类

2. 多模态附件优先预处理
   image / screenshot / voice / file
   -> aihubmix/gemini-3-flash-preview
   -> caption / OCR / 摘要 / 关键信息
   -> 合并回用户输入
   -> 继续路由

3. 强规则关键词判断
   舆情 / 危机公关 / 热点攻防 -> public_opinion
   slogan / 脚本 / campaign / 广告语 -> creative
   品牌战略 / 用户洞察 / 定位 / 人群 -> insight
   代码 / 报错 / 小脚本 -> simple_code

4. router LLM 辅助裁判
   使用 gemini/3.1-pro-xhigh
   只在高价值且规则冲突时触发
   输出固定 JSON 分类

5. 判断轻量还是重型
   轻量 -> set_provider() 直接答
   高价值前台 -> quota gate -> Gemini 3.1 Pro
   重型 -> quota gate -> Harness -> Hermes
```

注意：`gemini/3.1-pro-xhigh` 不能作为每条消息的默认门卫，否则 router 自身会消耗稀缺额度。

## 6. Ops Router 分类

`巅池-技术（DevOps）` 使用独立运维分类，不走 Business Router。

| intent | path | 说明 |
|---|---|---|
| `system_status` | 本地状态查询 | AstrBot / Hermes / Dashboard / 飞书连接状态 |
| `queue_status` | quota gate 查询 | 队列长度、当前运行任务、冷却时间 |
| `error_debug` | `codex/gpt-5.4` + 日志 | 报错解释、日志定位 |
| `code_ops` | `codex/gpt-5.4` | 代码问题、小修复建议 |
| `deployment_ops` | 本地脚本 / 状态检查 | 服务启动、重启、端口、进程 |
| `alert_review` | alert channel | 429、失败任务、飞书发送失败、Hermes 异常 |

Ops Router 不调用：

```text
gemini/3.1-pro-xhigh
claude-sonnet-4-6
claude-opus-4-7
```

除非后续单独设计“技术深度排障任务”，并经过独立审批规则。

## 7. Quota Gate 规则

稀缺资源采用保守策略：

```text
同一资源 max_concurrency = 1
任务完成后 cooldown = 30 分钟
冷却结束后才允许下一个任务开始
```

不是：

```text
任务开始后 30 分钟释放
```

而是：

```text
running -> complete / fail -> cooldown 30min -> available
```

资源池：

| resource_key | 用途 |
|---|---|
| `gemini_oauth_3_1_pro` | creative / insight / 高价值裁判 |
| `claude_oauth_global` | Claude OAuth 总闸门 |
| `claude_oauth_sonnet_4_6` | Sonnet 深任务 |
| `claude_oauth_opus_4_7` | Opus 高复杂度升级 |

Claude 如果使用同一 OAuth 账号池，必须同时占用：

```text
claude_oauth_global
claude_oauth_sonnet_4_6
```

或：

```text
claude_oauth_global
claude_oauth_opus_4_7
```

## 8. Harness 缰绳规则

Harness 只拴重任务，不拴所有消息。

不进 Harness：

- casual
- ops_writing
- multimodal
- realtime
- public_opinion
- simple_code
- creative / insight 的普通前台生成

进入 Harness：

- `deep_creative`
- `deep_insight`
- `#深度`
- `#PRD`
- 长报告
- 多资料分析
- 老板明确“不满意，继续深挖”
- 需要后台持续执行和回调的任务

Harness 职责：

- 记录任务生命周期。
- 防止 Hermes 跑飞。
- 支持后台查看、取消、重试、人工复核。
- 和飞书卡片联动，展示进度。
- 任务完成后回传原会话。

## 9. 飞书接入约束

### 9.1 platform_id 分流

正式接入时必须按 `event.get_platform_id()` 分流。

```text
platform_id == "巅池-Agent小助手"
  -> Business Router

platform_id == "巅池-技术（DevOps）"
  -> Ops Router

其他平台
  -> 不接管
```

### 9.2 飞书 API 客户端

所有飞书 OpenAPI 调用必须走：

```text
dc_engines/feishu_hub
```

不要在 router / harness 里重新创建一套 `lark.Client`。

已有文档要求：

```text
任何想跟飞书 API 打交道的代码 -> 都从 feishu_hub 走
```

### 9.3 飞书卡片

长回复、等待中、Hermes 进度卡片应复用：

```text
dc_engines/feishu_card_streamer
```

不要在新 router 里重复实现卡片协议。

## 10. 旧 Router 关闭策略

当前旧 router 在：

```text
data/plugins/llm_router/main.py
```

它已经会对部分飞书 platform 做 `set_provider()`，会和新 DC Router 冲突。

迁移策略：

```text
阶段 1：dry-run
  新 DC Router 只记录决策
  旧 llm_router 继续实际生效

阶段 2：shadow compare
  记录旧 router provider 与新 router decision 的差异
  不改变用户体验

阶段 3：新 router 接管小助手
  Business Router 成为巅池-Agent小助手唯一决策源
  旧 llm_router 对小助手关闭

阶段 4：接入 Ops Router
  巅池-技术进入运维专用路径
  旧 llm_router 对技术机器人关闭

阶段 5：清理旧 router
  旧 llm_router 保留代码但默认禁用
```

成功标准：

- 任意一条消息只有一个 router 能做最终 provider 决策。
- creative / insight 必须经过 quota gate。
- deep_* 必须经过 quota gate + Harness + Hermes。
- 巅池-技术不会触发业务 creative / insight / deep_*。
- 截图一定先走 `aihubmix/gemini-3-flash-preview`。
- 舆情一定走 `aihubmix/grok-4.3`。
- 简单代码一定走 `codex/gpt-5.4`。

## 11. 已创建的新代码骨架

新路径目录：

```text
router/
  taxonomy.py
  provider_map.py
  rules.py
  decision.py
  classifier.py
  entrypoint.py

harness/
  resources.py
  task_state.py
  queue_store.py
  quota_gate.py
  hermes_bridge.py
  callbacks.py
```

当前状态：

- 可导入。
- 可 dry-run。
- 未接入 AstrBot 主入口。
- 未关闭旧 router。
- quota gate 已支持 SQLite 队列和 30 分钟完成后冷却。

## 12. 下一步实施顺序

### Step 1：Business Router dry-run

只在 `巅池-Agent小助手` 上记录新 router 判定。

不做：

- `set_provider()`
- quota gate admission
- Harness 创建
- Hermes 派发

输出日志：

```text
platform_id
session_id
message_outline
router_intent
provider_id
requires_queue
requires_harness
source
reason
```

### Step 2：Ops Router dry-run

只在 `巅池-技术（DevOps）` 上记录运维分类。

重点验证：

- 状态查询。
- 队列查询。
- 报错 / 代码问题。
- 告警消息。

### Step 3：接管轻量 provider

只接管不排队任务：

- casual
- ops_writing
- multimodal
- realtime
- public_opinion
- simple_code

### Step 4：接管 Gemini OAuth 队列

接管：

- creative
- insight

要求：

- 单并发。
- 完成后冷却 30 分钟。
- 排队时给用户返回位置和预计时间。

### Step 5：接管 Harness + Hermes

接管：

- deep_creative
- deep_insight

要求：

- 创建 Harness task。
- 进入 quota gate。
- Hermes 后台执行。
- 飞书卡片显示进度。
- 完成后回传原会话。

### Step 6：关闭旧 router

确认新路径稳定后：

```text
旧 llm_router 不再对小助手 / 技术机器人做 set_provider()
```

## 13. 关键风险

| 风险 | 处理 |
|---|---|
| 两个 router 同时 set_provider | 新 router 接管时必须关闭旧 router 最终决策 |
| 3.1 Pro 被 router 裁判耗尽 | 只在高价值且规则冲突时调用 |
| Claude OAuth 被 Sonnet / Opus 绕过总额度 | 增加 `claude_oauth_global` 总闸门 |
| 长任务开始后 30 分钟错误释放 | 冷却从任务完成后开始 |
| 技术机器人误入业务 creative | 技术机器人走 Ops Router，不接 Business Router |
| 飞书 API client 分裂 | 所有飞书 API 走 `feishu_hub` |
| 用户等待无反馈 | 排队提示 + 飞书进度卡片 |

## 14. 官方约束参考

- Gemini API rate limits: <https://ai.google.dev/gemini-api/docs/rate-limits>
- Gemini API OAuth: <https://ai.google.dev/gemini-api/docs/oauth>
- Gemini 3.1 Pro Preview: <https://ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview>
- Gemini CLI: <https://developers.google.com/gemini-code-assist/docs/gemini-cli>
- Anthropic models: <https://docs.anthropic.com/en/docs/about-claude/models/all-models>
- Anthropic rate limits: <https://docs.anthropic.com/en/api/rate-limits>
- Claude Code security: <https://docs.anthropic.com/en/docs/claude-code/security>
- Anthropic OAuth examples: <https://docs.anthropic.com/en/api/oauth-examples>

