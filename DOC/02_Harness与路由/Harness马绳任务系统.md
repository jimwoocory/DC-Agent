# Harness 马绳任务系统

## 概述

Harness 是围绕「任务」的全生命周期管理系统，负责创建、追踪、完成任务，并将 LLM 的输出自动提升为可检索的记忆。

## 核心文件

| 文件 | 职责 |
|------|------|
| `astrbot/core/harness/engine.py` | HarnessEngine：任务状态机 |
| `astrbot/core/harness/task_store.py` | HarnessTaskStore：SQLite 持久化 |
| `astrbot/core/harness/memory_store.py` | HarnessMemoryStore：记忆存储 |
| `astrbot/core/harness/memory_promotion.py` | HarnessMemoryPromoter：LLM 响应 → 记忆 |
| `astrbot/core/harness/cognition.py` | HarnessCognitionProvider：认知快照（persona + KB + 历史任务）|
| `astrbot/core/harness/contracts.py` | 数据结构定义、HARNESS_TERMINAL_STATUSES |
| `astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py` | _maybe_complete_harness_task |

## 数据库

| 文件 | 内容 |
|------|------|
| `data/harness.db` | harness_tasks 表（任务记录） |
| `data/harness_memory.db` | harness_memories 表（提升后的记忆） |

## 任务状态机

```
pending → in_progress → completed
                     ↘ review_required → completed（审批通过）
                                       → in_progress（驳回重做）
       → failed（任何阶段可失败）
       → cancelled
```

终态（HARNESS_TERMINAL_STATUSES）：`completed`、`failed`、`cancelled`

## 任务闭环流程

```
/task intake marketing_plan Q2推广计划
       ↓
builtin_commands 创建 pending 任务
回复"请描述你的具体需求"
       ↓
员工发送需求描述
       ↓
LLM 生成完整方案（InternalAgentSubStage）
       ↓
_maybe_complete_harness_task() 自动触发
       ↓
complete_task(result={summary, response_preview})
       ↓
HarnessMemoryPromoter 将 summary 写入 harness_memory.db
```

## _maybe_complete_harness_task 逻辑

位于 `internal.py`，在 LLM 生成完成后异步触发：

1. `final_resp` 为空或 `completion_text` 为空 → 跳过
2. `harness_engine` 未初始化 → 跳过
3. `get_current_harness_task(umo)` 返回 None → 跳过
4. 任务已在终态 → 跳过
5. 截取前 200 字作为 summary，前 500 字作为 response_preview
6. 调用 `engine.complete_task()` → 触发记忆提升

## 记忆提升条件

`HarnessMemoryPromoter._build_summary()` 要求 `result.summary` 非空，才会写入 `harness_memory.db`。空 summary 不产生记忆记录。

## 认知快照（CognitiveSnapshot）

任务创建时，`HarnessCognitionProvider.build_snapshot()` 收集：
- 当前 session 绑定的 persona
- 绑定的知识库名称列表（从 `sp.session_get(session_id, "kb_config")`）
- 最近 5 条已完成任务摘要
- 最近 5 条 Memory 记录

快照写入任务 `payload.cognitive_context`，可在 `/task show <task_id>` 中查看。

## Workflow 类型说明

| workflow_kind | 领域 | 特点 |
|---------------|------|------|
| `marketing_plan` | 营销 | 完成后默认需要审查 |
| `content_delivery` | 内容交付 | 需要明确交付物字段 |
| `project_followup` | 项目跟进 | 需要跟进人和时间节点 |
| `approval_request` | 审批 | 需要审批人信息 |

## 常用命令

```
/task new               # 显示 workflow_kind 选择菜单
/task intake <kind> <brief>  # 创建 workflow 任务
/task ls                # 列出当前会话任务
/task show <task_id>    # 查看任务详情（含认知快照）
/task start <task_id>   # 标记为进行中
/task done <task_id>    # 手动完成（支持结果摘要）
/task fail <task_id>    # 标记失败
/task review <task_id>  # 进入审查状态
/task approve <task_id> # 审批通过
/task reject <task_id>  # 驳回
```

## 已知注意事项

**`should_call_llm(True)` 会阻断 LLM**：AstrBot pipeline 在 `event.call_llm = True` 时跳过 LLM。命名反直觉，不要在 task intake 后调用此方法来"触发"LLM，否则会卡死。

**僵尸 pending 任务**：测试阶段可能留下 pending 任务，直接在 `harness.db` 将 status 改为 `cancelled` 清除。

**知识库前置条件**：认知快照中的 KB 需要先在 dashboard 上传文档（目前所有 KB 为空），并在 session 配置中绑定 `kb_config.kb_ids`，才有检索内容。
