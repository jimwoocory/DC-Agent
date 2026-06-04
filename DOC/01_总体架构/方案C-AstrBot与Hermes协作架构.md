# 方案 C：AstrBot × Hermes 协作架构

确立时间：2026-04-24

## 核心决策

**AstrBot 是前台，Hermes 是后台。**

| 层 | 系统 | 职责 |
|----|------|------|
| 前台 | AstrBot | 接收消息、意图分类（Router）、任务创建（Harness）、即时回复、结果推送 |
| 后台 | Hermes | 执行复杂工作流、调用外部工具/API、多步骤 Agent 推理、知识密集型内容生成 |

## 消息路由规则

```
用户消息
    ↓
AstrBot Router 分类
    ├─ category=conversation  → AstrBot LLM 直接回复（不走 Harness）
    ├─ category=skill         → AstrBot Star Handler 处理
    └─ category=task（workflow）→ 创建 Harness 任务 → 派发给 Hermes 执行
```

**哪些任务派发给 Hermes：**
- 所有带 `workflow_kind` 的 Harness 任务（marketing_plan / content_delivery / project_followup / approval_request）
- 需要知识库检索 + 多轮推理的内容生成
- 需要调用外部 API 的技能任务（Google Workspace、GitHub、Linear 等）

**哪些任务留在 AstrBot LLM：**
- 普通对话（问答、寒暄、解释说明）
- 即时性强、不需要工具的简单请求

## 完整任务执行流程

```
1. 用户发消息："帮我做五菱五一推广方案"
           ↓
2. AstrBot Router 分类 → category=task, workflow_kind=marketing_plan
           ↓
3. HarnessEngine.create_task() 创建任务（status=pending）
           ↓
4. RouterStage 派发任务到 Hermes Webhook
   POST http://localhost:8644/webhooks/astrbot_task
   Body: { task_id, workflow_kind, brief, session_context, cognitive_context }
           ↓
5. AstrBot 立即回复用户："✅ 任务已提交，Hermes 正在分析，完成后我会通知你"
   （AstrBot 自己的 LLM 不介入）
           ↓
6. Hermes 异步执行（调用自身 LLM + 技能 + 知识库）
           ↓
7. Hermes 完成，POST 结果到 AstrBot
   POST http://localhost:8645/hermes_response
   Body: { task_id, unified_msg_origin, response, status }
           ↓
8. hermes_bridge 接收结果：
   - 调用 HarnessEngine.complete_task(task_id, result)
   - 调用 context.send_message(umo, response) 推送给用户
```

## 数据接口定义

### AstrBot → Hermes（任务派发）

```json
POST /webhooks/astrbot_task
{
  "task_id": "abc123",
  "workflow_kind": "marketing_plan",
  "brief": "五菱五一推广，25-35岁，小红书+抖音",
  "session_id": "qq_official:FriendMessage:62C924AF...",
  "unified_msg_origin": "癫池-测试:FriendMessage:62C924AF...",
  "cognitive_context": {
    "persona_id": "Biz_Assistant_Claw",
    "knowledge_base_names": ["品牌规范", "营销素材"],
    "recent_task_summaries": [...]
  }
}
```

### Hermes → AstrBot（结果回传）

```json
POST /hermes_response
{
  "task_id": "abc123",
  "unified_msg_origin": "癫池-测试:FriendMessage:62C924AF...",
  "response": "完整的营销方案内容...",
  "status": "completed"
}
```

## 与现有系统的变化

| 模块 | 变化 |
|------|------|
| `RouterStage._handle_task_intent()` | 创建任务后派发给 Hermes，阻断 AstrBot LLM |
| `hermes_bridge._handle_hermes_response()` | 新增：识别 task_id → 完成 Harness 任务 |
| `hermes_bridge` | 新增：`dispatch_task_to_hermes()` 方法 |
| AstrBot LLM pipeline | workflow 任务不再走 InternalAgentSubStage |

## 实现状态

| 功能 | 状态 |
|------|------|
| AstrBot Router → Harness 任务创建 | ✅ 已完成 |
| Harness 任务状态机 | ✅ 已完成 |
| Hermes 结果 → 用户推送（send_to_platform）| ✅ 已完成 |
| **RouterStage → Hermes 任务派发** | ✅ 已完成 |
| **Hermes 结果 → Harness task complete** | ✅ 已完成 |
| 知识库文档上传 | ⏳ 待配置（dashboard 操作） |
| Hermes 知识库检索配置 | ⏳ 待配置 |

## 知识库在方案 C 中的位置

知识库由 **Hermes 负责检索和使用**：

```
Harness 任务派发给 Hermes 时，携带 cognitive_context.knowledge_base_names
Hermes 根据 KB 名称检索相关内容，注入自己的 LLM prompt
生成带品牌知识的方案，回传 AstrBot
```

AstrBot 的 `kb_agentic_mode` 不再是主路径（留给普通对话的知识增强备用）。
