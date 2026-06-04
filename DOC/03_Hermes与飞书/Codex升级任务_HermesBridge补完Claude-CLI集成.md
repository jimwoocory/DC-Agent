# Codex 升级任务：补完 HermesBridge → Claude CLI 调用链

> **交接对象**：OpenAI Codex CLI
> **创建于**：2026-05-20
> **预估工时**：12–16h（含测试）
> **依赖**：`harness/quota_gate.py`（已交付）、`dc_engines/harness/*`（已交付）

---

## 0. 你必须先知道的几件事（避免上一轮的盲点）

上一轮你看了 `.claude/` 目录得出"DC-Agent 没有和 Claude CLI 深度集成"的结论。**那是 Claude Code 给自己存的配置目录**，与 DC-Agent 业务侧无关。集成代码全在 **`harness/` 和 `dc_engines/harness/`**，请直接看那里，不要再回到 `.claude/`。

集成的"地基"是有的：
- `harness/resources.py` 已经把 `CLAUDE_CLI_GLOBAL` / `CLAUDE_CLI_OPUS_4_7` 列为正式资源（含 cooldown 配置，今天刚调过）
- `harness/quota_gate.py` 是完整可用的配额闸门（aiosqlite 持久化、admission、ETA 估算）
- `harness/task_state.py`、`harness/queue_store.py` 状态/存储契约完整

**地基之上的洞**：`harness/hermes_bridge.py` 里 `HermesBridge.submit()` 是 `raise NotImplementedError("HermesBridge.submit is not wired yet")`。任务被 admit 之后没人真正执行——**这就是要你补的核心**。

---

## 1. 任务目标（一句话）

补完 `HermesBridge.submit()`，让被 `QuotaGate` 放行的任务能真正调起对应 runtime（**先实现 `claude_cli`**，其它 runtime 留 TODO），并在执行完成 / 失败 / 超时时通过 `TaskCallbackSink` 回写状态。

---

## 2. 强制阅读列表（按顺序）

读完再动手，不要跳读：

| 顺序 | 文件 | 你要看什么 |
|---|---|---|
| 1 | `harness/resources.py` | 资源键、cooldown 语义 |
| 2 | `harness/task_state.py` | `QueueJob` / `AdmissionDecision` / `QueueStatus` |
| 3 | `harness/queue_store.py` | 状态怎么持久化、有没有 `mark_completed` / `release_resource` 之类的方法 |
| 4 | `harness/quota_gate.py` | 你的上游，看它 admit 之后留下什么 hook、job_id 怎么传 |
| 5 | `harness/callbacks.py` | `TaskCallbackPayload` / `TaskCallbackSink` Protocol —— 你要 emit 这个 |
| 6 | `harness/hermes_bridge.py` | **你要改的文件**，目前只有 stub |
| 7 | `dc_engines/harness/__init__.py` + 同目录全部文件 | 上层引擎封装，看 `harness_engine` / `harness_store` 怎么被装配到 AstrBot context |
| 8 | `data/plugins/task_cli_plugin/main.py` | 真实调用方，理解 `create_workflow_request` 流向 |
| 9 | `data/plugins/hermes_bridge/` 整个目录 | AstrBot 侧的 hermes_bridge plugin（与 `harness/hermes_bridge.py` 同名不同物），看它怎么注入 engine/store |
| 10 | `DOC/Harness工程演进评估与全栈开发路线图_2026-05.md` | 整体路线图，重点看 §3.2 子系统盘点、§5 下一步方向、§6 Phase 0 / Phase 1 |
| 11 | `DOC/Hermes定位与双系统架构设计.md` | Harness 与 Hermes Agent 的职责边界 |
| 12 | `hermes-config/ANTHROPIC 配置指南.md` | 凭证位置 |

---

## 3. 实现要求

### 3.1 `HermesBridge.submit()` 的契约

签名保持不变：

```python
async def submit(self, request: HermesTaskRequest) -> str: ...
```

返回值约定：返回 `job_id`（与 `request.queue_job_id` 一致），异步执行在后台 task 里跑。

内部按 `request.payload["target_runtime"]` 分发（如果上游没传，从 `request.router_decision` 推断）：

| target_runtime | 实现方式 | 本轮要不要做 |
|---|---|---|
| `claude_cli` | spawn `claude` 子进程 | ✅ **必须** |
| `claude_oauth` / `anthropic_api` | Anthropic SDK 调用 | ⏸ 占位 TODO |
| `gemini_cli` | spawn `gemini` 子进程 | ⏸ 占位 TODO |
| `hermes_agent` | spawn `hermes-agent` CLI 或 HTTP | ⏸ 占位 TODO |

对未实现的 runtime，抛 `NotImplementedError` 并附明确文案（不要静默失败）。

### 3.2 Claude CLI 子进程调用细节

- **命令**：`claude -p <prompt> --output-format stream-json --verbose`
  （stream-json 是 Claude Code 唯一支持的机器可读输出格式）
- **stdin**：如果有多轮上下文，用 `--input-format stream-json` + stdin 喂 JSONL
- **环境变量**：
  - **不要**注入 `ANTHROPIC_API_KEY`——Claude CLI 走的是 `~/.claude/` 下的 OAuth（Claude Max 订阅），注入 API key 反而会切到按量计费
  - 可以注入 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` 减少噪音
- **异步**：用 `asyncio.create_subprocess_exec`，**不要**用 `subprocess.run`
- **超时**：硬上限取 `ResourceConfig.estimated_run_seconds × 3`，到点 SIGTERM；再宽限 5s 不退则 SIGKILL
- **输出解析**：逐行读 stdout，解析 stream-json，最后一条 type=`result` 的为最终回复
- **错误**：subprocess returncode != 0 / stream 中出现 `subtype="error_*"` / 超时 / 取消 —— 都走 FAILED 路径
- **并发**：同一资源键（如 `claude_cli_global`）`max_concurrency=1`，由 `QuotaGate` 保证；你**不**需要再加锁
- **资源释放**：执行完成（成功 / 失败 / 取消）后，调用 `QueueStore` 的对应方法把任务标完结、资源进入 cooldown。具体方法名以 `queue_store.py` 实际暴露的为准

### 3.3 回调

执行结束必须 emit 一次 `TaskCallbackPayload`：

```python
TaskCallbackPayload(
    job_id=request.queue_job_id,
    session_id=request.payload.get("session_id"),
    status="completed" | "failed",
    result={"text": "...", "raw": [...stream events...]} if ok else None,
    error="..." if not ok else None,
)
```

`HermesBridge` 构造时应接受一个 `TaskCallbackSink` 注入（依赖反转，不要在 bridge 内 hardcode 飞书或 dashboard）。

### 3.4 不要做的事

- ❌ 不要改 `harness/resources.py`（资源定义稳定，5-20 刚调过 cooldown，不要回退）
- ❌ 不要碰 `astrbot/` 核心目录（AstrBot 上游，与本任务无关；若改了会污染未来 upstream merge）
- ❌ 不要新建插件、不要在 `.claude/` 下加任何东西
- ❌ 不要写 `*_SUMMARY.md` / 完成报告（`AGENTS.md` 明令禁止）
- ❌ 不要污染 `~/.claude/settings.json` —— 那是用户本人的 Claude Code 配置
- ❌ 不要直接 import `anthropic` SDK 走 Claude CLI 路径（CLI 就是 CLI，不要混）

---

## 4. 工程纪律（仓库约定，必须遵守）

- Python 3.10+，`from __future__ import annotations`
- dataclass 用 `frozen=True, slots=True`（参考 `task_state.py`）
- 类型注解齐全，新代码必须能跑过 `ruff check .`
- 路径全用 `pathlib.Path`，引用 AstrBot 数据/临时目录走 `astrbot.core.utils.path_utils`
- 注释 / docstring **英文**
- 提交前：`ruff format .` && `ruff check .`
- Conventional commits，例如：`feat(harness): wire HermesBridge.submit for claude_cli runtime`
- PR title / body **英文**

---

## 5. 测试要求（验收硬门槛）

新增 `tests/harness/test_hermes_bridge.py`，至少覆盖：

1. `submit()` 不再抛 `NotImplementedError`（对 `target_runtime=claude_cli`）
2. Mock `asyncio.create_subprocess_exec`，断言：
   - 命令行参数包含 `claude`、`-p`、`--output-format stream-json`
   - 不包含 `ANTHROPIC_API_KEY` 环境注入
3. 模拟正常 stream-json 输出，断言最终 `TaskCallbackPayload.status == "completed"` 且 `result["text"]` 正确
4. 模拟 returncode=1，断言 `status == "failed"` 且 `error` 非空
5. 模拟超时，断言 SIGTERM 被发送、`status == "failed"`、错误信息含 "timeout"
6. 对未实现的 `target_runtime`（如 `gemini_cli`），断言抛 `NotImplementedError` 且文案明确

跑测：`uv run pytest tests/harness/ -v`。

---

## 6. 边界 —— 这些不归你管

- **Router 决策**（怎么决定用 claude_cli 还是 anthropic_api）：上游负责，你只读 `request.payload["target_runtime"]`
- **飞书消息回传**：`TaskCallbackSink` 的实现方负责，你只 emit payload
- **Claude CLI 凭证管理**：假设 `~/.claude/` 已配好（用户已经在用 Claude Max 订阅）
- **Hermes Agent 子进程**：本轮留 TODO，给个清晰的 NotImplementedError 即可
- **Gemini 路径**：同上

---

## 7. 你需要知道的环境

- macOS 15.1，Python 由 `uv` 管理
- 启动：`uv sync` → `uv run main.py`
- 仓库根：`/Users/dianchi/DC-Agent`
- Claude CLI 可执行：`which claude` → `/Users/dianchi/.claude/local/claude`（或 PATH 中的全局）
- `hermes-config/.env` 有 `ANTHROPIC_API_KEY`，**但 Claude CLI 路径不要用它**

---

## 8. 完成定义（DoD）

- [ ] `harness/hermes_bridge.py` 实现完成，`submit()` 对 `claude_cli` runtime 工作
- [ ] `tests/harness/test_hermes_bridge.py` 全部通过
- [ ] `ruff format .` && `ruff check .` 无报错
- [ ] 端到端联通一次：在 dev AstrBot 实例对 `/task intake marketing_plan "测试用例"` 触发，能看到 Claude CLI 进程起来、回调写回、任务状态正确
- [ ] PR 标题 conventional commits，PR body 英文，**不**附 `*_SUMMARY.md`

---

## 9. 如果你遇到这些情况

- **`queue_store.py` 没暴露你需要的方法**（如 `mark_completed` / `release_resource`）：先加上，作为本 PR 的一部分，保持最小化（不要顺手重构）
- **`HermesTaskRequest` 缺字段**（比如没有 `target_runtime`）：在 `hermes_bridge.py` 同文件内扩展，向后兼容（新字段给默认值）
- **凭证文件找不到**：报清晰错误 `Claude CLI credentials not found at ~/.claude/`，不要回落到 API key
- **不确定上游期望**：优先看 `data/plugins/task_cli_plugin/main.py` 是怎么用 store 的，对齐它的预期

---

## 10. 一句话总结你的任务

> 把 `HermesBridge.submit()` 从 stub 改成能真正 spawn `claude` CLI、等结果、回调状态、释放资源 —— 别动地基，别动 AstrBot 上游，别动用户的 `.claude/` 配置。
