# Router 意图路由系统

## 概述

Router 是消息进入 LLM 之前的意图分类层，负责将用户消息分发到正确的处理路径（创建任务、触发技能、或走普通对话）。

## 核心文件

| 文件 | 职责 |
|------|------|
| `astrbot/core/router.py` | IntentRouter：规则匹配 + LLM fallback 分类 |
| `astrbot/core/router_config.yaml` | 意图规则配置（pattern/keywords + confidence） |
| `astrbot/core/pipeline/process_stage/router_stage.py` | RouterStage：pipeline 中的路由执行层 |

## 工作流程

```
消息入站
   ↓
RouterStage.route(event)
   ↓
IntentRouter.classify(message)
   ├─ pattern 精确匹配（confidence 高）
   ├─ keywords 关键词匹配（confidence 中）
   └─ LLM fallback（confidence < fallback_threshold 时）
   ↓
分类结果
   ├─ category=task   → 创建 Harness 任务（workflow_kind 已确定）
   ├─ category=skill  → 激活对应 Star 插件 handler
   └─ category=conversation → 不干预，走正常 LLM pipeline
```

## 置信度阈值

`fallback_threshold: 0.75`（在 `router_config.yaml` 顶部）

- confidence ≥ 0.75：直接按规则执行
- confidence < 0.75：先走 LLM fallback 二次确认，再决定是否执行

## 意图规则类型

### pattern 匹配（精确，高置信度）

```yaml
- pattern: "/task new"
  confidence: 0.99
  category: "task"
  intent_type: "task_new"
```

### keywords 匹配（模糊，中置信度）

```yaml
- keywords: ["营销计划", "推广计划", "营销方案"]
  confidence: 0.70
  category: "task"
  intent_type: "marketing_plan"
  workflow_kind: "marketing_plan"
```

### skill 路由（通用）

```yaml
- pattern: "生成图片"
  confidence: 0.97
  category: "skill"
  intent_type: "dreamina_image"
  skill_name: "dreamina_plugin"
  command_name: "生成图片"
```

## 已配置的 workflow_kind

| workflow_kind | 触发关键词示例 |
|---------------|---------------|
| `marketing_plan` | 营销计划、推广方案、渠道规划 |
| `content_delivery` | 内容交付、交付物、deliverables |
| `project_followup` | 项目跟进、follow-up |
| `approval_request` | 审批请求、发起审批 |

## 已配置的 skill_name

| skill_name | 触发示例 |
|------------|----------|
| `dreamina_plugin` | 生成图片、画一张、做个视频 |
| `powerpoint` | 做PPT、生成幻灯片 |
| `github` | github issue、代码审查 |
| `google-workspace` | google docs、谷歌文档 |
| `research-paper-writing` | 写论文、学术写作 |
| `find-nearby` | 附近餐厅、找附近 |
| `linear` | linear issue、linear任务 |
| `obsidian` | obsidian笔记 |
| `hermes-agent` | hermes配置、hermes怎么用 |

## 重要设计决策

**`/task intake` 不走 Router**：`builtin_commands` Star handler 优先拦截 `/task intake` 命令，Router 的 task intake 规则是死规则，已从配置中移除。

**keyword confidence 统一设为 0.70**：低于 fallback_threshold（0.75），关键词命中后进 LLM 二次确认，避免误触发创建任务。

**task_new 由 Router 处理**：用户发 `/task new` 时，Router 返回 workflow_kind 选择菜单，不创建任务。实际创建通过 `/task intake <kind> <brief>` 完成。

## 扩展：新增意图规则

在 `router_config.yaml` 的对应 section 追加条目，重启生效。无需改代码。

LLM fallback 的 system_prompt 中维护了 `allowed skill_name` 列表，新增 skill 后记得同步更新。
