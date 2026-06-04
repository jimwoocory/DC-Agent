# W2 / 2A-2 Cowork Prompt Draft — 任务提取和提醒

> **目的**：W1 完成合并后立即派给 cowork，避免每次再写 prompt 的延迟。
>
> **派发命令**：`spawn_task(title=..., tldr=..., prompt=<下方内容>, isolation="worktree")`

---

## Chip Title

`W2 / 2A-2: 任务提取和提醒（员工最强需求 91%）`

## Chip TL;DR

群里 `@DC-Agent 提醒` / `汇总待办` 触发，AI 从最近聊天里抽出结构化任务（事项+负责人+截止时间+状态），落到当前 Case 的 Harness task 台账；支持 `/tasks` 查看 + 定时提醒。Codex 问卷 91%（10/11）员工选作最强需求。

---

## Prompt（完整）

```text
你是一个在 DC-Agent 项目（/Users/dianchi/DC-Agent）开发的 Claude Code 远程 agent。本次任务是 W2 / 2A-2：任务提取和提醒。

## 业务背景

Codex 二期需求问卷里 **10/11（91%）** 员工把"任务提取和提醒"列为最强需求。痛点：项目群里"X 周三前给我那份方案"、"Y 帮忙跟一下甲方反馈"等任务散落在聊天里，员工事后翻群找、漏跟进。

参考文档：
- [DOC/DC-Agent二期统一方案_2026-05.md](../../DOC/DC-Agent二期统一方案_2026-05.md) §四 W2 行
- [deliverables/DC-Agent二期需求对比报告.pdf](../../deliverables/DC-Agent二期需求对比报告.pdf) 表三 "任务提取和提醒 10/11 (91%)"
- W1 / 2A-1 已交付的 group_summary 模块（参考其 LLM-JSON 设计 + Router 集成范式）
- W0 / 2A-0 已交付的 Case 聚合层（必须复用：每个抽出的任务自动挂到当前 active case）

## 你要做的事

### 1. 触发机制（双入口）

**入口 A**：群里 @DC-Agent + 任务相关关键词
- `@DC-Agent 提醒今天的任务`
- `@DC-Agent 汇总待办`
- `@DC-Agent 抽一下任务`
- `@DC-Agent 我的待办`
- 关键词集合：["提醒", "待办", "任务列表", "汇总待办", "抽任务", "抽待办"]
- 在 `astrbot/core/router_config.yaml` 加 task_intents 规则（intent_type=task_extract，category=skill，confidence 0.95+）

**入口 B**：CLI 命令（直接查询，不走 LLM）
- `/tasks` — 列当前 case / session 的活跃任务
- `/tasks all` — 含已完成
- `/tasks @<user>` — 仅显示某人的任务

### 2. 任务提取（LLM + 结构化输出）

新增模块 `astrbot/core/task_extractor/`，结构参考 W1 group_summary：

```python
# astrbot/core/task_extractor/contracts.py
@dataclass
class ExtractedTask:
    description: str       # 事项描述
    assignee: str | None   # 负责人（"@张三" / "我" / None）
    assignee_user_id: str | None  # 解析后的内部 user_id
    deadline: datetime | None  # 截止时间（已解析为 datetime）
    deadline_raw: str      # 原始时间表达（"周三前" / "明天" / "5月20日"）
    priority: Literal["high", "normal", "low"]
    status: Literal["pending", "in_progress", "done"]  # 抽取时默认 pending
    source_message_ts: str  # 来源消息时间戳
    confidence: float      # LLM 提取置信度
```

```python
# astrbot/core/task_extractor/extractor.py
async def extract_tasks_from_messages(
    messages: list[dict],
    *,
    llm_provider,
    time_now: datetime,  # 用于解析"明天"等相对时间
    user_directory: dict[str, str] | None = None,  # 群成员名 → user_id 映射
) -> list[ExtractedTask]:
    ...
```

**LLM Prompt 模板**（system_prompt）：

```
你是企业项目群聊任务抽取助手。给你一组聊天记录，请抽出其中包含的"待办任务"。
一条聊天可能不含任务、含一个任务、或含多个任务。

只返回 JSON 数组，每个任务对象字段：
{
  "description": "事项描述（一句话）",
  "assignee_hint": "原文中的人名提示（@张三/我/小李/null）",
  "deadline_raw": "原文中的时间表达（周三前/明天/5月20日/null）",
  "priority": "high|normal|low",
  "confidence": 0.0-1.0
}

判定标准：
- 仅抽具体可执行的待办（"周三前给我方案"）
- 跳过抽象/已完成的（"已经做完了"）
- 跳过咨询性陈述（"我们应该考虑..."）
- 优先级：明确说"急/尽快"=high，明确说"不急/有空"=low，其他=normal
- 不确定时返回空数组

不返回任何解释文字，只返回 JSON。
```

**后处理**（不交给 LLM）：
- `assignee_hint` → 通过 `user_directory` 或群成员匹配解析 `assignee_user_id`，匹配不到留 None
- `deadline_raw` → 用 dateutil + 自写规则解析 datetime（"今天/明天/后天/周一-周日/N 天后/具体日期"），解析失败留 None

### 3. 任务落地（Case 聚合层）

抽出来的任务**自动落到当前 active case** 的 Harness task store：

```python
for ex_task in extracted_tasks:
    h_task = await harness_engine.create_task(
        HarnessTaskCreateRequest(
            title=ex_task.description[:80],
            conversation_id=conversation_id,
            platform_id=platform_id,
            session_id=session_id,
            domain="task_extract",
            payload={
                "extracted_by": "task_extractor_v0",
                "assignee_user_id": ex_task.assignee_user_id,
                "assignee_hint": ex_task.assignee_hint,
                "deadline": ex_task.deadline.isoformat() if ex_task.deadline else None,
                "deadline_raw": ex_task.deadline_raw,
                "priority": ex_task.priority,
                "source_message_ts": ex_task.source_message_ts,
                "confidence": ex_task.confidence,
            },
        )
    )
    # 软挂到当前 case
    if active_case:
        await case_engine.attach_task(active_case.case_id, h_task.task_id)
```

### 4. /tasks 查询命令

新增 `astrbot/builtin_stars/builtin_commands/commands/tasks.py`（仿 case.py）：

- `/tasks` — 当前 session 的 active case 中所有 pending/in_progress 任务，按 deadline 排序
- `/tasks all` — 含 completed/cancelled
- `/tasks @<user>` — 按 assignee 过滤
- `/tasks @me` — 自己的任务
- 输出 markdown 表格：事项 | 负责人 | 截止 | 优先级 | 状态

### 5. 提醒机制（CronManager 集成）

复用 `astrbot/core/cron_manager.py`。在抽取任务时**自动创建提醒 cron job**：

```python
if ex_task.deadline:
    # 截止前 1 天提醒一次，截止当天提醒一次
    cron_mgr.add_one_time(
        ts=ex_task.deadline - timedelta(days=1),
        callback=_remind_task,
        args=(h_task.task_id, "提前 1 天"),
    )
    cron_mgr.add_one_time(
        ts=ex_task.deadline,
        callback=_remind_task,
        args=(h_task.task_id, "今日截止"),
    )
```

`_remind_task` 通过 hermes_bridge 的 callback dispatcher 把提醒推回原群 / 私聊 @ 对应负责人：

```
[任务提醒 - 今日截止]
事项：周三前给客户方案初稿
负责人：@张三
状态：pending
进度回复：/task complete <task_id> 或在此回复
```

### 6. Case 自动连通

跟 W1 一样：抽出 task 后挂到当前 case，提醒也挂到 case 的 events 流。

### 7. 测试

新增：
- `tests/unit/test_task_extractor.py` — extract_tasks_from_messages 纯函数（mock LLM，至少 12 case：单任务、多任务、空消息、各种 deadline 格式、各种 priority、解析失败容错、user_directory 匹配）
- `tests/unit/test_deadline_parser.py` — 时间解析单独测试（"今天"/"明天 9:00"/"周三"/"5/20"/"下周一"/"3 天后"/带年份/无效输入 ≥ 15 case）
- `tests/unit/test_tasks_command.py` — /tasks CLI 各种过滤
- `tests/integration/test_task_extract_flow.py` — 端到端：mock 聊天 → 抽 → 落 Case → /tasks 查得到

## 关键参考文件

- `astrbot/core/group_summary/` —— W1 / 2A-1 已交付，看它的模块结构（contracts/extractor/__init__.py）和 LLM JSON 调用范式直接复用
- `astrbot/core/harness/contracts.py` + `engine.py` —— Harness task store（不改 schema，只用 create_task）
- `astrbot/core/case/engine.py` —— Case 聚合层（attach_task）
- `astrbot/core/cron_manager.py` —— 提醒调度
- `astrbot/plugins/hermes_bridge/__init__.py` + `data/plugins/hermes_bridge/hermes_bridge.py` —— Callback dispatcher 用法
- `astrbot/builtin_stars/builtin_commands/commands/case.py` + `tasks.py`(本次新增) —— CLI 范式

## 验收

1. 新增测试 ≥ 30 case 全过
2. **所有现有测试不破**（合并后 master 跑 `pytest tests/ -q`）
3. ruff check 干净
4. `scripts/smoke_startup_check.py` 通过
5. router bench 加 3-5 个 task_extract 触发样本，整体 ≥ 95%
6. 提交 `changelogs/2A2_task_extraction.md`

## 工作流

- 用 git 分支：`feat/2a2-ta${REDACTED_API_KEY}`
- commit message 风格：`feat(skills): add task_extractor v0 (W2 / 2A-2)`
- **Phase 1**（先做）：模块骨架 + 接口签名 + 给出 5+ 设计偏差让我 review → 等 "继续 Phase 2"
- **Phase 2**：实现 + 测试 + changelog
- 完成后回报：测试通过情况、commit hash、文件清单、有无遗留

## 不要做的事

- 不要碰 W0 G2 / W0 2A-0 / W1 group_summary 已合并的代码（必要时只读不写）
- 不要默认抢答群消息（必须 @DC-Agent + 关键词触发，跟 W1 一样的低打扰原则）
- 不要把提醒发到错的群（只回原对话的 session_id）
- 不要 commit 巨型 fixture（mock 用 inline dict）
- 暂不实现：跨 case 任务聚合、依赖关系（A 完成才能 B）、子任务、提醒频率自定义——这些是 W2 之后的优化
```

---

## 派发清单

W1 合并后执行：

```python
mcp__ccd_session__spawn_task(
    title="W2 / 2A-2: 任务提取和提醒（员工最强需求 91%）",
    tldr="群里 @DC-Agent 提醒/汇总待办 → AI 抽结构化任务挂到当前 Case 的 Harness 台账，CronManager 自动提醒。Codex 91% 员工最强需求。",
    prompt=<本文档 ## Prompt 节>,
    isolation="worktree",
)
```

---

## 与 W1 / W0 的衔接

- 复用 W1 的 LLM-JSON 模板设计、Router skill 集成、history fetch 模式
- 复用 W0 2A-0 的 Case 聚合（attach_task 是 Case engine 已有方法）
- 复用 W0 G2 的 Hermes callback dispatcher（用于推送提醒）

## 风险与未知

- LLM 抽取准确率：v0 不做 fine-tune，靠 system_prompt 模板；预计 70-85% 准确（需后续 dataset 验证）
- 时间解析覆盖：中文相对时间表达组合多，需要持续扩 dateutil 规则
- 提醒推送的认领回复：v0 让用户手写"完成"，不做按钮（按钮需要平台插件支持，飞书可行、QQ 限制大）
