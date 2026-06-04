# Codex 评估任务：飞书交互式 Skill 运维卡片

## 背景

Hermes / skill / harness 线已经完成第一版核心闭环：

- 普通群聊 / 私聊会话蒸馏
- 老板 skill / 同事 skill 生成
- skill 列表、查看、质量审阅
- 人工纠错沉淀
- 版本备份、回滚
- 软删除、已删除列表、恢复
- 危险操作管理员权限与 `--confirm` 确认
- harness guardrails 注入
- 对应测试覆盖

现在需要评估下一步产品化：把飞书里的 skill 运维从“纯文本命令回复”升级为“可操作的飞书交互卡片”。

## 重要澄清

这次要做的不是“项目状态总结卡”。

错误方向示例：

- 一张标题类似“查询同事和老板.skill蒸馏”的总结卡
- 展示“现在已经有 / 后续增强空间”
- 底部放泛泛的“审阅 / 查看 / 回滚 / 恢复”按钮
- 按钮没有绑定具体 skill、slug、version、kind

正确方向：

卡片必须服务于真实 skill 运维。每个按钮都应绑定具体操作对象和参数，例如：

- `kind=boss`
- `slug=杨总`
- `version=v3`
- `action=review`

点击按钮后应能触发后端对应操作，或进入确认卡，而不是只展示静态说明。

## 现有命令

目前 HermesBridge 已支持文本命令：

```text
/chat list-bosses
/chat list-colleagues
/chat deleted-bosses
/chat deleted-colleagues

/chat inspect-boss <slug>
/chat inspect-colleague <slug>

/chat review-boss <slug>
/chat review-colleague <slug>

/chat correct-boss <slug> <修正内容>
/chat correct-colleague <slug> <修正内容>

/chat boss-rollback <slug> <version> --confirm
/chat colleague-rollback <slug> <version> --confirm

/chat delete-boss <slug> --confirm
/chat delete-colleague <slug> --confirm

/chat restore-boss <slug>
/chat restore-colleague <slug>
```

相关代码入口：

- `data/plugins/hermes_bridge/hermes_bridge.py`
- `tests/harness/test_hermes_bridge.py`
- `dc_engines/dc_engines/feishu_card_streamer/`

## 目标

评估并设计飞书交互式 skill 运维卡片方案，优先覆盖以下四类卡片：

1. Skill 列表卡
2. Skill 详情卡
3. Skill 质量审阅卡
4. 已删除 Skill 回收站卡

危险操作必须走确认卡。

## 卡片需求

### 1. Skill 列表卡

触发命令：

```text
/chat list-bosses
/chat list-colleagues
```

预期展示：

- 每个 skill 一行或一个 compact block
- 展示：
  - name
  - slug
  - version
  - corrections_count
  - updated_at
- 每个 skill 对应按钮：
  - 查看
  - 审阅
  - 纠错
  - 回滚
  - 删除

按钮必须携带该行 skill 的 `kind`、`slug`、当前 `version`。

### 2. Skill 详情卡

触发命令：

```text
/chat inspect-boss <slug>
/chat inspect-colleague <slug>
```

预期展示：

- name
- slug
- kind
- version
- conversation_type
- message_count
- speaker_count
- corrections_count
- updated_at
- knowledge_sources
- 文件清单

按钮：

- 审阅
- 纠错
- 回滚
- 删除

### 3. Skill 质量审阅卡

触发命令：

```text
/chat review-boss <slug>
/chat review-colleague <slug>
```

预期展示：

- score
- 通过项
- 风险项
- corrections_count
- 建议动作

按钮：

- 查看详情
- 纠错
- 删除
- 回滚

### 4. 已删除 Skill 回收站卡

触发命令：

```text
/chat deleted-bosses
/chat deleted-colleagues
```

预期展示：

- name
- slug
- deleted_at
- deleted_path

按钮：

- 恢复
- 查看备份路径

恢复按钮必须绑定具体 `kind` 和 `slug`。

## 危险操作确认卡

以下操作必须先展示确认卡，不能单击列表按钮后直接执行：

- 删除
- 回滚

确认卡展示：

- 操作类型
- kind
- slug
- version，如适用
- 风险说明
- 操作者信息

按钮：

- 确认执行
- 取消

确认执行后才调用现有后端逻辑：

```text
/chat delete-boss <slug> --confirm
/chat boss-rollback <slug> <version> --confirm
```

恢复操作也需要管理员校验，但不要求二次确认，因为当前后端不会覆盖已有同名 skill。

## 权限要求

沿用现有后端权限：

- `skill_admin_only`
- `skill_admin_user_ids`
- AstrBot `event.is_admin()`

卡片按钮不能绕过后端权限。

前端卡片可以隐藏危险按钮，但不能只依赖隐藏按钮做安全控制。

## 交互实现需要评估

请 Codex 评估当前仓库里飞书卡片交互能力：

1. 当前 `dc_engines.feishu_card_streamer` 是否支持按钮 action 回调？
2. AstrBot Lark adapter 是否能接收飞书卡片按钮点击事件？
3. 如果已有 action 回调入口，应该接到 HermesBridge 的哪个方法？
4. 如果没有回调入口，最小实现路径是什么？
5. 是否需要新增一个 card action router，例如：

```text
data/plugins/hermes_bridge/card_actions.py
```

或直接放在：

```text
data/plugins/hermes_bridge/hermes_bridge.py
```

## 建议技术方案

优先复用现有文本命令的业务逻辑，不重复写删除、恢复、回滚、审阅、查看代码。

建议新增内部 action payload：

```json
{
  "source": "hermes_skill_card",
  "action": "review",
  "kind": "boss",
  "slug": "杨总",
  "version": "v3"
}
```

后端收到按钮事件后映射到现有方法：

- `inspect` -> `_inspect_skill_bundle`
- `review` -> `_review_skill_bundle`
- `delete_request` -> 发送删除确认卡
- `delete_confirm` -> `_conversation_skill_delete` 或底层 `_soft_delete_skill_bundle`
- `rollback_request` -> 发送回滚确认卡
- `rollback_confirm` -> `_rollback_skill_bundle`
- `restore` -> `_restore_deleted_skill_bundle`

## 验收标准

### 功能验收

- `/chat list-bosses` 能返回 skill 列表卡，而不是纯文本
- `/chat list-colleagues` 能返回 skill 列表卡
- `/chat inspect-boss <slug>` 能返回详情卡
- `/chat review-boss <slug>` 能返回质量审阅卡
- `/chat deleted-bosses` 能返回回收站卡
- 点击“审阅”能进入对应 skill 的审阅结果
- 点击“查看”能进入对应 skill 的详情
- 点击“删除”先进入确认卡
- 点击“回滚”先进入确认卡
- 点击“恢复”能恢复对应已删除 skill，且仍受管理员权限控制

### 安全验收

- 非管理员点击删除、回滚、恢复会被拒绝
- 删除和回滚不能绕过确认
- 按钮 payload 被篡改时，后端仍按权限和路径校验拒绝非法操作
- 不允许通过 slug 构造访问 skill 根目录之外的路径

### 测试验收

至少补充：

- 卡片 payload 生成测试
- inspect card 内容测试
- review card 内容测试
- deleted card 内容测试
- delete request 生成确认卡测试
- delete confirm 权限测试
- rollback confirm 权限测试
- restore 权限测试

现有测试必须继续通过：

```text
uv run pytest tests/harness/test_hermes_bridge.py -q
uv run pytest dc_engines/tests/test_harness_lifecycle.py -q
uv run ruff check ...
uv run ruff format --check ...
```

## 非目标

本轮不要做：

- 后台 Dashboard 管理
- 自动合并多轮纠错生成新版 skill
- 大范围重构 HermesBridge
- 替换现有文本命令

文本命令仍保留，卡片只是更好的交互层。

## 需要 Codex 输出

请先评估，不要直接大改。输出：

1. 当前飞书卡片 / 按钮回调能力现状
2. 最小可落地方案
3. 需要修改的文件清单
4. 风险点
5. 分阶段实施建议
6. 如果可以直接实现，给出第一阶段 patch 范围

