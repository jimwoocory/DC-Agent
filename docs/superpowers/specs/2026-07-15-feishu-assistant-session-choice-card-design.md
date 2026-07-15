# 飞书小助手会话去向确认卡设计

## 目标

在飞书一对一聊天中，小助手完成并送达一项正式任务结果后，发送一张会话去向确认卡，让用户明确选择：

- `开启新对话`：在同一个飞书聊天窗口中创建新的 AstrBot conversation，后续消息不再自动携带上一项任务的短期上下文。
- `继续当前对话`：保留当前 AstrBot conversation，后续消息继续沿用本次任务的短期上下文与资源。

飞书原生的 `新消息` 分割线继续承担视觉提示职责；它不是机器人可读取的事件，也不能作为后端切换会话的依据。真正的会话边界由服务端的持久化决策记录表达。

## 产品边界

### 适用范围

首版仅覆盖飞书私聊中，由小助手工作台正式发起且成功完成的任务。任务结果必须先完整送达，确认卡随后单独发送。

以下场景不发送确认卡：

- 日常闲聊、菜单导航、表单编辑或任务提交状态。
- 仍在运行、等待补充、失败或取消的任务。
- 群聊以及无法确认唯一操作者的会话。
- 后台任务或未绑定当前飞书事件的任务结果。

这张卡不是执行任务前的第二次审批。工作台保存仍然是唯一的执行边界，确认卡只决定已经完成的任务之后如何处理短期会话上下文。

### 会话术语

- **飞书聊天**：稳定的私聊窗口和 `chat_id`，两个选择都不创建新的飞书群或聊天。
- **AstrBot conversation**：模型短期对话历史的逻辑容器，由 conversation UUID 标识。
- **Harness task**：本次已经完成的工作任务。无论选择哪种会话去向，该任务的完成状态都不得被改写。
- **UMO/session**：当前平台用户与聊天范围的统一消息来源标识，用于定位当前 AstrBot conversation。

## 交互设计

### 待选择卡

卡片使用 Card JSON 2.0，并沿用现有小助手卡片视觉体系：

- 标题：`这项任务已完成`
- 正文：`接下来要开启新对话，还是继续当前对话？`
- 说明：`开启新对话后，历史消息仍可查看，但后续消息不再自动带入上一项任务的短期上下文。`
- 主按钮：`开启新对话`
- 次按钮：`继续当前对话`

按钮回调只携带可信路由所需的 `source`、`action` 和不可猜测的 `decision_id`。任务、会话和操作者信息由服务端根据 `decision_id` 查询，不能信任客户端回传这些字段。

### 终态卡

按钮处理成功后，原卡片原位更新并移除操作按钮：

- 新对话终态：`新对话已开启`，说明 `你的下一条消息将从全新的短期上下文开始。`
- 继续终态：`已继续当前对话`，说明 `你的下一条消息将继续沿用本次任务的上下文。`

若用户未点击卡片而直接发送下一条文本，系统默认选择 `继续当前对话`，并尽力把原卡更新为继续终态。系统不设置会自动清空上下文的超时默认值。

## 状态模型

每个完成任务创建一个 `assistant_session_decision` 记录。一个 UMO 同一时刻最多保留一条可操作的待选择记录。

| 状态 | 含义 | 可执行动作 |
| --- | --- | --- |
| `pending` | 卡片已创建，等待用户决定 | 新对话、继续或直接发消息 |
| `opening_new` | 已接受新对话选择，正在创建或核对目标 conversation | 仅允许幂等重试和恢复 |
| `new_conversation` | 新 conversation 已生效 | 无 |
| `continue_current` | 当前 conversation 被保留 | 无 |
| `superseded_continue` | 后续完成结果产生了更新的确认卡，旧卡按继续处理 | 无 |

创建新确认卡时，服务端在同一个数据事务中把该 UMO 旧的 `pending` 记录更新为 `superseded_continue`，再写入新记录。旧卡随后尽力更新为不可操作终态，防止用户通过过期卡切换掉更新后的工作上下文。

## 持久化数据

在现有 Harness SQLite 存储中增加专用决策表，不复用 Harness work context 中表示平台聊天的 `conversation_id` 字段。

每条记录至少包含：

- `decision_id`：主键和按钮幂等键。
- `task_id`：唯一绑定的已完成 Harness task；同一任务只能生成一条决策。
- `unified_msg_origin`、`platform_id`：定位消息会话和 AstrBot conversation manager。
- `source_conversation_id`：发卡时的 AstrBot conversation UUID。
- `target_conversation_id`：终态实际使用的 conversation UUID；待选择时为空。
- `card_message_id`：原确认卡的飞书消息 ID。
- `state`：上述状态机状态。
- `operator_id`：实际点击按钮的用户 ID；自动继续时记录触发消息的用户 ID。
- `decision_source`：`card_click`、`implicit_message` 或 `superseded`。
- `created_at`、`decided_at`、`updated_at`：审计时间。
- `card_patch_state`：卡片终态同步状态，用于失败后的补偿更新。

数据库约束保证 `task_id` 唯一。状态更新使用带旧状态条件的 compare-and-set，确保同一个 `decision_id` 只有第一个有效选择生效。

## 运行流程

### 结果完成后发卡

1. 小助手按现有流程完成工作台任务并先发送最终结果。
2. `after_message_sent` 阶段确认事件来自飞书私聊、带有工作台任务标记，并且绑定的 Harness task 已成功完成。
3. 服务端读取当前 UMO 的 AstrBot conversation UUID。
4. 在 Harness 存储中关闭旧待选择记录，并为当前 `task_id` 创建新的 `pending` 决策。任务唯一约束阻止重复发卡。
5. 构建并发送 Card JSON 2.0 确认卡，把返回的飞书 `message_id` 绑定到决策记录。
6. 发卡失败不撤销已经送达的任务结果；决策保留为可重试投递状态并记录错误，不伪装成成功。

### 选择“继续当前对话”

1. 可信卡片回调路由根据 `decision_id` 加载记录，并验证回调来自原私聊和合法操作者。
2. 服务端以 compare-and-set 将 `pending` 更新为 `continue_current`。
3. `target_conversation_id` 写入 `source_conversation_id`，当前 UMO 的 conversation 映射保持不变。
4. 原卡片通过飞书消息 patch 接口更新为继续终态并移除按钮。
5. 重复点击读取并返回首次终态，不再次修改会话。

### 选择“开启新对话”

1. 可信卡片回调路由完成相同的记录、聊天和操作者校验。
2. 服务端以 compare-and-set 将 `pending` 更新为 `opening_new`，从这一刻起其他选择不能覆盖它。
3. conversation manager 为同一个 UMO 创建新的 AstrBot conversation，并保留当前平台配置及适用的人设设置。
4. 新 UUID 写入 `target_conversation_id`，决策更新为 `new_conversation`；旧 Harness task 仍保持已完成。
5. 原卡片更新为新对话终态并移除按钮。下一条用户消息按 UMO 当前映射进入新 conversation。

如果进程在 `opening_new` 阶段中断，恢复逻辑先比较 UMO 当前 conversation 与 `source_conversation_id`：已经发生切换时，将当前 UUID 补写为目标并完成决策；仍未切换时才重试创建。这样可避免重放回调创建多个新 conversation。

### 用户直接发送下一条消息

消息进入正常路由前，服务端查询该 UMO 最新的 `pending` 决策，并以 `implicit_message` 来源原子更新为 `continue_current`。随后消息继续使用原 conversation，原确认卡异步更新为继续终态。这个动作不改变用户消息内容，也不把决策说明写进对话历史。

## 一致性和错误处理

业务状态是事实源，卡片只是它的投影：

- 先提交决策和 conversation 映射，再 patch 卡片。
- 业务事务失败时保持卡片可操作，并返回可重试的错误提示。
- 业务已提交但卡片 patch 失败时不得回滚会话选择；记录 `card_patch_state` 并由后续回调或补偿流程重试。
- 重复点击、回调重放和网络重试都返回首次生效的结果。
- 已过期、已被替代或归属不匹配的卡片不能切换当前会话。
- 发送新确认卡或 patch 旧卡失败时不得调用 LLM 生成替代内容。

## 代码落点

- `dc_engines/dc_engines/assistant_workbench_cards.py`：新增待选择和终态卡片构建逻辑，并注册 `assistant_session_choice` 卡型。
- `dc_engines/dc_engines/harness/task_store.py`：新增决策表、查询、compare-and-set 和补偿状态更新。
- `dc_engines/dc_engines/harness/contracts.py`：新增对应的类型与状态约束。
- `data/plugins/dc_router/main.py`：在结果发送后的生命周期阶段创建确认卡；在普通消息进入任务路由前处理隐式继续。
- `data/plugins/dc_router/preprocessing/card_action.py` 和 `assistant_workbench.py`：路由并执行两个可信按钮动作。
- `harness/contracts/feishu_assistant_workbench.json`：登记新卡型和闭环验收标准。

实现应沿用现有模块结构，简单逻辑保持内联；只在持久化操作或三处以上复用时增加独立辅助方法。

## 验收标准

1. 成功完成的飞书私聊工作台任务在结果送达后只产生一张确认卡。
2. 任务结果发送不被确认卡阻塞，且确认卡不构成第二次执行审批。
3. 卡片恰好包含 `开启新对话` 和 `继续当前对话` 两个动作，并使用 Card JSON 2.0。
4. 选择新对话后，UMO 映射到新的 AstrBot conversation UUID；下一条消息不携带上一 conversation 的短期历史。
5. 选择继续后，UMO 仍映射到原 UUID；下一条消息继续使用当前短期历史。
6. 未点击而直接发消息时，决策按 `continue_current` 闭环，用户消息照常进入当前 conversation。
7. 同一个 `decision_id` 的重复点击不会创建第二个 conversation，也不会覆盖首次选择。
8. 新结果产生后，旧的待选择卡不能再改变会话。
9. 卡片 patch 失败不回滚已经生效的会话选择，并保留可重试的同步状态。
10. 闲聊、菜单、提交状态、失败任务、取消任务和群聊不发送此卡。
11. 决策审计能够追溯任务、UMO、操作者、原/目标 conversation、选择来源、卡片消息和时间。

## 验证

- 卡片模板测试：验证 JSON 2.0、两个按钮、可信 payload 和两个终态均无可操作按钮。
- 工作台路由测试：验证触发范围、结果先于确认卡、任务去重、可信回调和非目标场景。
- Harness 存储测试：验证唯一约束、compare-and-set、旧卡替代和审计字段。
- conversation manager 集成测试：验证新对话只创建一次，继续动作不创建新 UUID，`opening_new` 可恢复。
- 消息路由测试：验证直接发消息会先隐式继续，且不污染用户消息和对话历史。
- 故障测试：验证业务失败保持待选择，卡片 patch 失败保留已生效业务状态。
- 运行 `scripts/agent-check.sh --profile targeted`，再在原生飞书客户端分别验证两个按钮和直接发消息三条路径。
