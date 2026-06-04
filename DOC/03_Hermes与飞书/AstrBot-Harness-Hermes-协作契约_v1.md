# AstrBot / Harness / Hermes 三层协作契约 v1

最后更新：2026-05-29

## 1. 单一职责

| 层 | 职责 | 不负责 |
|----|------|--------|
| AstrBot 前台 | 接入 IM 平台、执行插件 hook、发送消息、维护当前会话上下文、展示飞书卡片 | 长任务状态机、深度执行、跨任务持久记忆的核心规则 |
| Harness 中台 | 统一创建任务、推进任务状态、记录 append-only event、写入任务记忆、管理稀缺模型队列 | 直接发送 IM 消息、直接操作 AstrBot pipeline stage、承载具体模型执行细节 |
| Hermes 执行 | 执行深度任务、回传中间态和最终结果、提供后台执行能力 | 直接改 Harness 数据库、直接改 AstrBot 内部状态、绕过回调契约发散状态 |

## 2. 允许的跨层调用白名单

| 调用方向 | 当前允许方式 | 入口文件 |
|----------|--------------|----------|
| AstrBot 插件 → Harness | 只通过 `context.harness_engine` / `context.harness_store` 的公开方法访问，例如 `create_task`、`complete_task`、`fail_task`、`append_trace`、`list_tasks_for_session` | `data/plugins/hermes_bridge/hermes_bridge.py`、`data/plugins/task_cli_plugin/main.py`、`data/plugins/workflow_intent_plugin/main.py`、`data/plugins/harness_sensor_plugin/main.py` |
| Harness sidecar 注入 → AstrBot 插件上下文 | `hermes_bridge.initialize()` 可创建 `HarnessTaskStore`、`HarnessMemoryStore`、`HarnessEngine`，并挂到 `context.harness_engine` / `context.harness_store` | `data/plugins/hermes_bridge/hermes_bridge.py` |
| AstrBot LLM hook → Harness 记忆 | `@filter.on_llm_request` 只读 Harness memory，并追加到 `ProviderRequest.system_prompt` | `data/plugins/hermes_bridge/hermes_bridge.py` |
| AstrBot LLM response → Harness 任务完成 | `@filter.on_llm_response` 只能完成 `source == "router_intent"` 且仍为 `pending` 的任务 | `data/plugins/hermes_bridge/hermes_bridge.py` |
| Harness runtime → AstrBot/Dashboard 回调 | 通过 `TaskCallbackSink.send(TaskCallbackPayload)` 回传任务状态；释放配额通过 `QuotaGate.complete/fail` | `harness/hermes_bridge.py`、`harness/callbacks.py` |
| Hermes → AstrBot | 通过 hermes_bridge 响应服务和 HMAC 校验后的回调入口，按 `task_id` 完成/失败 Harness task，并按 `session_key` / UMO 回推用户 | `data/plugins/hermes_bridge/hermes_bridge.py` |
| 飞书卡片状态同步 | `context.feishu_stream_map[task_id]` 只保存卡片消息句柄；卡片更新不得作为任务状态唯一来源 | `data/plugins/hermes_bridge/hermes_bridge.py`、`dc_engines/feishu_card_streamer/` |

## 3. 禁止行为

- 禁止插件直接修改 AstrBot `Stage`、`InternalAgentSubStage` 或 pipeline 调度顺序。
- 禁止业务插件绕过 Harness 直接派发深度任务给 Hermes；深度任务必须先产生 Harness task 或 queue job。
- 禁止 Hermes 直接写 `data/harness.db`、`data/harness_memory.db` 或 AstrBot 会话库。
- 禁止把 `context.harness_engine` / `context.harness_store` 当普通 dict 写入任意字段；新增共享状态必须先定义契约。
- 禁止把飞书卡片终态当作 Harness 终态替代品；卡片 finalize 后仍必须调用 Harness 状态方法。
- 禁止在文档或配置中提交生产 secret、真实用户 token、真实白名单或运行时数据库。

## 4. 状态同步策略

| 状态对象 | 主键 | 主存储 | 同步规则 |
|----------|------|--------|----------|
| Case | `case_id` | `dc_engines.case` store | Case 只保存业务归档和关联 `task_ids`，不直接推进任务状态 |
| Harness Task | `task_id` | `data/harness.db` | 所有任务状态以 HarnessTaskStore 为准；事件通过 `append_event` 追加 |
| Harness Memory | `memory_id` / `task_id` | `data/harness_memory.db` | 只由 `HarnessMemoryPromoter` 从已完成任务结果生成 |
| Quota Queue Job | `job_id` | QuotaGate SQLite store | 稀缺模型准入、冷却、失败和释放只通过 `QuotaGate` |
| Hermes Session | `session_key` | `data/hermes_sessions.db` | 只由 `SessionRouter` 维护 platform user 与 Hermes session 的映射 |
| 飞书卡片流 | `task_id -> message_id` | 进程内 `context.feishu_stream_map` | 只用于 UI 更新；进程重启后不得作为恢复状态的唯一依据 |

状态推进的最低要求：

1. 新业务任务先创建 Harness task，再触发 LLM / Hermes / 卡片流。
2. Hermes 中间态只写卡片或 trace，不得完成 task。
3. Hermes 最终成功调用 `complete_task`，最终失败调用 `fail_task`。
4. AstrBot 直接 LLM 回答只允许完成 `router_intent` 来源的 pending task。
5. 终态任务不得继续 `append_trace`。

## 5. 解耦路线图

| 优先级 | 目标 | 说明 |
|--------|------|------|
| P0 | typed context adapter | 把动态的 `context.harness_engine` / `context.harness_store` 收敛成显式 adapter，减少插件随意读写 |
| P0 | callback sink 标准化 | Hermes、飞书卡片、Dashboard 状态回调统一落到 `TaskCallbackSink` 风格契约 |
| P1 | 记忆注入迁出插件 | 将 `on_llm_request` 中的 Harness memory scoring 迁到 `dc_engines.harness`，插件只负责 hook 编排 |
| P1 | 状态完成策略迁出插件 | 将 `on_llm_response` 的 pending task 完成规则抽成 Harness service，避免业务规则散在插件中 |
| P2 | 静态边界检查 | 用 `scripts/validate_architecture.py` 检查禁止依赖和危险跨层访问 |

## 6. 变更影响评估 Checklist

任何跨层变更合并前必须回答：

- 是否新增了 AstrBot 插件直接访问 Harness 或 Hermes 的入口？
- 是否只调用公开方法，而不是直接改 SQLite、内部 dict 或私有字段？
- 是否改变了 `task_id`、`session_key`、`unified_msg_origin` 的映射关系？
- Hermes 中间态和最终态是否仍能区分？
- 飞书卡片失败时，Harness task 是否仍能进入正确终态？
- 是否新增 secret/config/runtime 文件进入 git？
- 是否有目标测试覆盖 Router 决策、Harness 状态、Hermes 回调或卡片行为？
