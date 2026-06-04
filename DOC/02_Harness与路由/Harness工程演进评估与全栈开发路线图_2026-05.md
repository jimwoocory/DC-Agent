# DC-Agent Harness 工程演进评估与全栈开发路线图

> 报告日期：2026-05-09
> 适用版本：DC-Agent（AstrBot v4.24.2 + Hermes Agent v0.8.0）
> 作者：Claude Opus 4.7（基于代码盘点 + 2026 年文献综述）

---

## 一、研究目标与范围

本报告完成三件事：

1. **对标 SOTA**：系统梳理 2026 年最新 Harness Engineering（智能体外壳工程）的学术与工程文献，提炼当前业界共识的"九大组件"与设计模式。
2. **现状评估**：对 DC-Agent 仓库（`hermes-agent`、`astrbot`、`astrbot-hermes-bridge`、`dashboard`、`harness-engineering-phase1-plan.md` 等）做全栈盘点，识别已具备能力、半成品与盲区。
3. **路线图制定**：以"差距分析矩阵"为输入，给出**4 条全栈主线 × 4 个 Phase**的详细开发计划，包含任务粒度、工时估算、验收标准。

---

## 二、Harness Engineering 文献综述（2026）

### 2.1 共识定义

> **Agent = Model + Harness**。Harness 是"模型之外的一切"——智能体循环、工具调用、上下文管理、记忆、护栏、追踪。Anthropic 在 2026 年正式将 *Claude Code SDK* 更名为 *Claude Agent SDK*，正是把"harness 作为运行时"这一抽象升格为产品形态。

参见：[Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)、[Harness engineering — Martin Fowler / Birgitta Böckeler](https://martinfowler.com/articles/harness-engineering.html)。

### 2.2 业界共识：生产级 Harness 的九大组件

来自 [awesome-harness-engineering](https://github.com/ai-boost/awesome-harness-engineering) 与 [MindStudio: What Is an Agent Harness](https://www.mindstudio.ai/blog/what-is-agent-harness-architecture-explained)：

| # | 组件 | 关键代表 |
|---|------|---------|
| 1 | Model Interface | Anthropic / OpenAI / Gemini SDK 多供应商抽象 |
| 2 | Tool Registry | MCP 协议、JSON Schema、Pydantic + `instructor` |
| 3 | Context Manager | Prompt Caching、LLMLingua、自主压缩 |
| 4 | Planning Module | Plan-and-Execute、LATS、TaskWeaver |
| 5 | Execution Engine | LangGraph、Agents SDK、CrewAI Flow |
| 6 | Memory System | Letta(MemGPT)、mem0、MemPalace、MAGMA |
| 7 | Feedback Loop | Guides（前馈：linter/类型/policy）+ Sensors（反馈：测试/评测） |
| 8 | Safety Guardrails | OAP、五层权限、Auto Mode 二级分类器 |
| 9 | Orchestration Layer | Sub-agent handoff、CrewAI 双层、ADK |

### 2.3 Anthropic 关于"长跑型 Agent"的核心结论

来自 [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) 与 [Harness design for long-running apps](https://www.anthropic.com/engineering/harness-design-long-running-apps)：

- **跨上下文窗口的接力**：把每次会话视为"一班工程师"，必须在交接处留下 `init.sh` + `progress.txt` + git commit + 200+ 项功能清单（带 pass/fail）。**仅靠 compaction 是不够的**。
- **失败模式四象限**：① 过早完工（premature completion）② 环境劣化 ③ 测试不充分 ④ 探索开销。每一项都需要 harness 层显式机制。
- **工具设计**：浏览器自动化（Puppeteer/Playwright MCP）显著提升验证能力，但 alert modal 等仍是盲区——**verification 工具的覆盖度=harness 能力上限**。
- **"一次只做一个 feature"** 是核心策略，比单次大改动更稳。

### 2.4 Martin Fowler 框架：Guides + Sensors

[Harness engineering（Böckeler / martinfowler.com）](https://martinfowler.com/articles/harness-engineering.html) 给出统一的控制论模型：

- **Guides（前馈）**：在错误发生*之前*引导——类型系统、linter、fitness function、prompt 约束。
- **Sensors（反馈）**：在动作*之后*观测——测试、运行时遥测、AI 语义评审。
- 二者各有"计算式（毫秒级、确定性）"与"推理式（LLM 语义、慢但富）"两类实现。
- 三类治理维度：**Maintainability harness / Architecture-fitness harness / Behaviour harness**。
- 关键洞见：**Keep quality left**——越早检测越便宜；以及**harness coverage** 应像代码覆盖率一样可度量（这是当前的开放问题）。

### 2.5 学术前沿（2026）

- [**OctoBench (arXiv:2601.10343)**](https://arxiv.org/abs/2601.10343)：首个**Scaffold-Aware**评测基准。34 个环境、217 个任务、7098 条二元清单项，覆盖 Claude Code/Kilo/Droid 三类 scaffold。核心结论：**任务解决能力 ≠ 对 scaffold 规则的遵从能力**——必须把 harness 本身当成被测对象。
- [**AutoHarness (arXiv:2603.03329)**](https://arxiv.org/abs/2603.03329)：把 harness 合成视为**优化目标**，用 Gemini-2.5-Flash 自动迭代生成代码 harness，使小模型在 145 个 TextArena 游戏中**0 非法动作**并跑赢更大的 Gemini-2.5-Pro。证明：**Harness 提升的边际收益 ≥ 模型升级**。
- [**Natural-Language Agent Harnesses (arXiv:2603.25723)**](https://arxiv.org/html/2603.25723v1)：用自然语言（而非代码）描述 harness 状态机，便于跨模型迁移。
- 工程信号：**Terminal Bench 2.0** 上"仅改 harness"把 agent 从第 30 名拉进 Top 5（[awesome-harness-engineering](https://github.com/ai-boost/awesome-harness-engineering)）；微软 Azure SRE Agent 在 35,000+ 生产事件中把 MTTM 从 40.5 分钟降到 3 分钟。

### 2.6 一句话提炼

> **2026 年的研究主线是：把 harness 视为"被评测、被合成、被优化"的一等工件，而非纸面架构图。**

---

## 三、DC-Agent 现状评估

### 3.1 系统拓扑（事实）

参见 [SYSTEM_ARCHITECTURE.md](../SYSTEM_ARCHITECTURE.md) 与 [SESSION_HANDOFF.md](../SESSION_HANDOFF.md)：

```
QQ / Feishu / WebChat
        │
        ▼
   AstrBot (port 6185, v4.24.2)
   ├─ RouterStage (intent classify)
   ├─ ProcessStage → InternalAgentSubStage
   ├─ Harness sidecar (SQLite per-session)
   └─ Hermes Bridge (HMAC webhook 8644/8645)
        │
        ▼
   Hermes Agent (localhost:8787, v0.8.0)
   ├─ Skills (26 顶级、80 嵌套 SKILL.md)
   └─ Knowledge Base 4 路 (NAS SMB)
        │
        ▼
   Vue Dashboard (port 3000, hatch-bundled)
```

### 3.2 Harness 子系统逐项盘点

| 维度 | 现状 | 文件锚点 |
|------|------|----------|
| **Agent Loop** | 无独立 loop；LLM 在 InternalAgentSubStage 内单轮调用，Harness 仅做事后记录 | `astrbot/core/harness/engine.py`、[harness-engineering-phase1-plan.md](../harness-engineering-phase1-plan.md) |
| **Tool Registry** | AstrBot Star 插件机制；`mcp>=1.8.0` 已引入但 AstrBot 侧未使用 | `pyproject.toml:31`、`astrbot/core/star/star_manager.py` |
| **Context Manager** | Lossless Context 压缩器在 v4.24.2 合并丢失，已重新接回（+65 行） | [UPGRADE_SMOKE_REPORT_2026-05-09.md](../UPGRADE_SMOKE_REPORT_2026-05-09.md) |
| **Planning** | 无独立 Planner；Phase-1 计划列了 `HarnessOrchestrator` 但**未启动** | `harness-engineering-phase1-plan.md:1-414` |
| **Execution Engine** | `HarnessEngine.create_task / mark_in_progress / complete_task` 三态机；缺主动驱动 | `astrbot/core/harness/engine.py:16-80` |
| **Memory** | 三层雏形：task event log、session SQLite、`memory_promoter` 长期晋升；**晋升链路休眠**（无上游调用方填充 result_json） | `harness/memory_store.py`、`memory_promotion.py:31-50` |
| **Feedback / Sensor** | 657 单测通过；满意度跟踪 `harness/satisfaction.py`；**无运行时 sensor、无评测基准** | `tests/unit/test_harness_*` |
| **Safety** | JWT + HMAC + session 隔离；**无 RBAC、无 permission policy、无审计** | `astrbot/core/router.py`、`hermes_bridge/router.py` |
| **Observability** | Logger + 启动 smoke check；**无 trace、无 metric 端点、无评测 dashboard** | `scripts/smoke_startup_check.py` |
| **Orchestration** | Hermes 是外部执行体而非 sub-agent；无 handoff/选民/审稿模式 | `hermes_bridge/__init__.py` |
| **Router/Intent** | 规则匹配 + LLM fallback（< 0.75 置信度）；intent 草案已铺开但 LLM fallback 未完全接通 model provider | `astrbot/core/router.py:61-104`、[ROUTER_INTENTS_DRAFT.md](../ROUTER_INTENTS_DRAFT.md) |
| **Frontend** | Vue dashboard（hatch 打包到 data/dist），KB 管理、任务面板；**无 trace 视图、无 harness 可视化** | `dashboard/`、`astrbot/dashboard/routes/knowledge_base.py` |
| **Deployment** | compose.yml + k8s（astrbot / astrbot_with_napcat 两套）+ NAS sync watchdog | `compose.yml`、`k8s/`、`nas_sync/` |
| **Tech Stack** | Python 3.12；anthropic / openai / google-genai；faiss + bm25；mcp 1.8 | `pyproject.toml:7-68` |

### 3.3 Phase-1 计划已承诺但未交付

[harness-engineering-phase1-plan.md](../harness-engineering-phase1-plan.md) 共 5 件交付物（13–19h 预算）：

1. `HarnessOrchestrator`（主动编排层）
2. `HarnessToolInterceptor`（工具调用前后校验）
3. `WorkflowEngine`（可执行工作流，非模板）
4. 接入 `InternalAgentSubStage`
5. `QualityCheckpoint` 系统（对齐性 / 工具使用 / 完整性）

**当前状态**：未启动。

### 3.4 主要技术债（从 SESSION_HANDOFF / UPGRADE_SMOKE_REPORT 抽取）

- 记忆晋升链路 dormant：`engine.complete_task()` 调用方仅 `/task complete` CLI，实际 LLM 路径不喂结果。
- Hermes Bridge `_send_to_qq()` 标记 TODO，无 webhook 重试 / 队列缓冲。
- 测试有硬编码绝对路径已修，但仍需检查 CI 一致性。
- OpenClaw 5 旧插件 + `jw_astrbot_shell` 已归档但残留引用。
- 知识库索引仍在跑 watcher，doc_count 验证未完成。

---

## 四、差距分析矩阵

> 评分：✅ 已具备 / 🟡 半成品 / 🔴 缺失。SOTA 列对应 §2.2 九大组件 + Fowler 维度 + 学术评测。

| 维度 | DC-Agent | SOTA 锚点 | 评级 | 关键差距 |
|------|----------|-----------|------|---------|
| Model Interface | 多 SDK 已装 | Claude Agent SDK 抽象 | 🟡 | 缺统一 ModelClient 抽象、无 fallback / 路由策略 |
| Tool Registry | Star 插件 + MCP 依赖 | MCP 标准 + 输出约束 | 🟡 | 工具未走 MCP，schema 非 Pydantic 强约束，错误消息未为 LLM 优化 |
| Agent Loop | 单轮 | ReAct / LangGraph 状态机 | 🔴 | 无多轮工具调用循环、无 thought trace |
| Context Mgmt | Lossless 重接 | Compaction + Cache + 自主压缩 | 🟡 | 无 prompt cache、无分级摘要、无跨会话接力文件 |
| Planning | 无 | Plan-and-Execute / TaskWeaver | 🔴 | 无 planner / decomposition |
| Memory | 三层雏形 | Letta / mem0 / MemPalace | 🟡 | 长期晋升链路未触发，无语义检索、无冲突治理 |
| Sub-agent / Orchestration | Hermes 外部 | Handoff / CrewAI Flow | 🔴 | 无 sub-agent 生成、无 reviewer agent |
| Sensors（反馈） | 单测 + 满意度 | Fitness / OTEL / LLM-as-judge | 🟡 | 无运行时 sensor，无 LLM-judge 评审 |
| Guides（前馈） | 类型 + ruff + pre-commit | Linter + policy + 类型 | ✅ | 基本到位 |
| Permission / Auth | JWT + HMAC | OAP / Auto Mode 二级分类 | 🟡 | 无 policy DSL、无审计日志、无人在回路升级 |
| Observability | Log + smoke | OTEL + trace + eval dashboard | 🔴 | 无 trace span、无 token 计费、无失败回放 |
| Eval / Benchmark | 单测 | OctoBench / SWE-Bench / 自建 | 🔴 | 无回归 eval、无 scaffold-aware checklist |
| Frontend | Vue dashboard + KB | Trace UI + Skill marketplace | 🟡 | 无 harness 时间线、无 token / cost 视图 |
| Router | 规则 + LLM fallback | LLM 二级分类 | 🟡 | LLM provider 未挂、置信度阈值未调 |
| Skills | 80 个 SKILL.md | Anthropic Skills + 评测 | 🟡 | 无 skill 评测、无版本治理 |
| Long-Running 协议 | 无 | progress.txt + init.sh + 功能清单 | 🔴 | 无跨班接力契约 |

**结论**：DC-Agent 已经搭好"骨架与外设"，但**主动 harness 内核（loop / planner / orchestrator / interceptor / eval）几乎全空**，恰好是 2026 年文献的高价值区。Phase-1 计划方向正确但范围保守，需要再补 3 件事：**评测体系、跨班接力、可观测性**。

---

## 五、下一步全栈开发方向

### 5.1 北极星

> **让 DC-Agent 成为"被评测、可观测、能跨班接力"的企业级 harness 平台**——用 OctoBench 风格的 scaffold-aware checklist 持续度量自身、用 Anthropic 长跑模式做企业流程接力、用 Fowler 的 guides+sensors 做质量回路。

### 5.2 设计原则（强约束）

1. **Harness-as-Artifact**：harness 行为本身要有版本、有评测、有 changelog（参考 OctoBench）。
2. **Two-Speed Sensors**：毫秒级计算式 + 秒级 LLM-judge，分别挂在 pre-commit / post-step / nightly。
3. **Cross-Session Contract**：每个长任务必须在 Hermes 工作区落 `progress.md` + `init.sh` + `feature_list.json`。
4. **MCP First**：新工具一律以 MCP server 形式登记；旧 Star 插件渐进迁移。
5. **Memory 主动晋升**：`complete_task` 必须由编排器（而非 CLI）触发，附 result_json。

### 5.3 四条全栈主线

| 主线 | 后端 | 前端 | 评测 |
|------|------|------|------|
| **L1 Harness 内核** | Orchestrator + Interceptor + Planner + Sub-agent | 时间线 trace 视图 | scaffold checklist |
| **L2 上下文与记忆** | Compaction、Prompt Cache、mem0 风格三层、跨班接力 | 记忆浏览器 + 重要度热力 | LongMemEval 子集 |
| **L3 评测与可观测** | OTEL trace、token/cost 表、LLM-judge | Eval dashboard、回归对比 | OctoBench-DC（自建） |
| **L4 安全与治理** | Permission DSL、审计日志、HITL 升级 | 待审批面板、policy 编辑器 | 红队 / ATBench 子集 |

---

## 六、详细实施计划

> 估算前提：1 名全栈工程师 + 1 名前端 + Claude Code 协助；周维度排期。

### Phase 0｜地基修复（1 周，~25h）

| # | 任务 | 工时 | 验收 |
|---|------|------|------|
| 0.1 | 完成 Phase-1 计划 5 件原始交付物（Orchestrator / Interceptor / WorkflowEngine / 接入 / QualityCheckpoint） | 16h | 47 单测全绿 |
| 0.2 | 修复 `complete_task` 上游不喂 result_json 的链路 | 3h | `data/harness_memory.db` 出现非空记录 |
| 0.3 | Hermes Bridge `_send_to_qq` + webhook 重试 + 死信队列 | 4h | 重试 3 次、超时落 DLQ、有指标 |
| 0.4 | Router LLM fallback 接通 provider，置信度阈值可配置 | 2h | 测试新增 6 个置信度边界 case |

### Phase 1｜Harness 内核升级（3 周，~80h）

| # | 任务 | 工时 | 验收 |
|---|------|------|------|
| 1.1 | 多轮 Agent Loop（ReAct 风格），落 thought / action / observation 三段 | 12h | 工具循环 ≥ 5 轮稳定、有 timeout 与 max-step |
| 1.2 | `Planner`：把意图拆为 sub-task DAG，写入 `harness_plan` 表 | 10h | 4 类 workflow_kind 各有规划样例 |
| 1.3 | `SubAgentSpawner`：reviewer agent + tester agent 两类首发 | 14h | reviewer 给出结构化评审 JSON、tester 调用 Playwright MCP |
| 1.4 | `ToolInterceptor` 增强：pre-call policy + post-call sensor + 失败重试 | 8h | 注入式单测 100% 拦截非法工具调用 |
| 1.5 | MCP 服务首发：dreamina / minimax / KB query 三件迁移 | 16h | `mcp list` 显示三件、Hermes 与 AstrBot 双向调用通 |
| 1.6 | Skill 版本与签名（hash + manifest） | 8h | 80 个 skill 全有 manifest，CI 校验 |
| 1.7 | Cross-Session Contract：Hermes 工作区每任务 `progress.md` / `init.sh` / `feature_list.json` 自动生成 | 12h | 重启 agent 能基于 progress 续跑 |

### Phase 2｜上下文与记忆（2.5 周，~60h）

| # | 任务 | 工时 | 验收 |
|---|------|------|------|
| 2.1 | Anthropic Prompt Cache 接入（system prompt + tools） | 6h | 缓存命中率 dashboard ≥ 60% |
| 2.2 | 自主 Compaction 策略（按 token 预算 + 重要度） | 14h | 长会话 token 占用下降 ≥ 40% |
| 2.3 | 三层记忆重写（core/archival/recall），FAISS + BM25 双路检索 | 16h | LongMemEval 自选 50 题 R@5 ≥ 75% |
| 2.4 | 记忆冲突治理（mem0 风格 decay + dedupe） | 10h | 冲突测试集 pass |
| 2.5 | 知识库整合：把 4 路 KB 注册成统一 retriever，热更新 | 14h | doc_count > 0 验证、watcher 实时同步 |

### Phase 3｜评测与可观测（2 周，~50h）

| # | 任务 | 工时 | 验收 |
|---|------|------|------|
| 3.1 | OTEL Trace 接入（每个 tool call / LLM call / sub-agent 一个 span） | 10h | Jaeger / Tempo 可见完整 trace |
| 3.2 | Token & Cost Meter（按 model / session / tenant 聚合） | 6h | dashboard 出现成本曲线 |
| 3.3 | **OctoBench-DC**：自建 scaffold-aware checklist（≥ 200 项），覆盖 4 类 workflow | 18h | nightly 跑通、给出 pass-rate |
| 3.4 | LLM-as-Judge sensor：每个完成任务自动评分（5 维：对齐 / 工具 / 完整 / 风格 / 安全） | 10h | 评分写入 `harness_review` 表 |
| 3.5 | 回归评测 CI（PR 阻塞）：阈值下降 > 5pp 拒绝合并 | 6h | 一次故意降级被 CI 拦截 |

### Phase 4｜安全治理与前端（2 周，~50h）

| # | 任务 | 工时 | 验收 |
|---|------|------|------|
| 4.1 | Permission Policy DSL（YAML，五层 allow/deny + 模式） | 12h | 100% 工具调用走 policy、审计日志 |
| 4.2 | HITL 升级：高风险动作自动停下并推送审批 | 8h | QQ/飞书/WebUI 三端审批回调 |
| 4.3 | Dashboard：Harness 时间线视图（trace + token + 评分三合一） | 14h | 单任务可回放、跨任务可对比 |
| 4.4 | Dashboard：评测看板（OctoBench-DC pass-rate + 趋势 + 失败样例） | 10h | 周报自动出图 |
| 4.5 | 红队 mini-bench：注入 / 越权 / 工具误用 30 例 | 6h | 阻断率 ≥ 90% |

### 总预算

| Phase | 周 | 工时 | 关键产出 |
|-------|----|------|---------|
| Phase 0 | 1   | 25h | 地基修复 + Phase-1 原计划 |
| Phase 1 | 3   | 80h | Harness 内核 + MCP 化 + 跨班接力 |
| Phase 2 | 2.5 | 60h | 上下文 + 记忆 + KB |
| Phase 3 | 2   | 50h | 评测 + 可观测 |
| Phase 4 | 2   | 50h | 安全 + 前端 |
| **合计** | **10.5 周** | **265h** | 全栈升级 |

---

## 七、风险与依赖

| 风险 | 等级 | 缓解 |
|------|------|------|
| AstrBot v4.24.2 与上游再合并冲突（Lossless Context 已被冲掉过） | 高 | 把 harness 改动隔离成独立模块 + 合并前 smoke check |
| Hermes 是独立服务、协议变化破坏 bridge | 中 | 协议加版本头、Bridge 走 contract test |
| MCP 迁移导致旧 Star 插件回归 | 中 | 双跑期 2 周、灰度切换 |
| OctoBench-DC 自建工作量被低估 | 中 | 复用 MiniMaxAI/OctoBench 的 checklist 格式 |
| 记忆系统改写影响线上会话 | 高 | 双写 + 影子读 4 周后再切主 |
| LLM-judge 偏置 | 中 | 每周 50 条人审校准 |

**强依赖**：Anthropic / OpenAI / Gemini API 配额、NAS SMB 稳定性、aihubmix embedding 服务。

---

## 八、参考文献

### 工程实践
- [Effective harnesses for long-running agents — Anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [Harness design for long-running application development — Anthropic](https://www.anthropic.com/engineering/harness-design-long-running-apps)
- [Harness engineering for coding agent users — Martin Fowler / Birgitta Böckeler](https://martinfowler.com/articles/harness-engineering.html)
- [Claude Code Agent Harness: Architecture Breakdown — WaveSpeed](https://wavespeed.ai/blog/posts/claude-code-agent-harness-architecture/)
- [What Is an Agent Harness? — MindStudio](https://www.mindstudio.ai/blog/what-is-agent-harness-architecture-explained)
- [Inside the Claude Agents SDK — ML6](https://www.ml6.eu/en/blog/inside-the-claude-agents-sdk-lessons-from-the-ai-engineer-summit)
- [Agent Harness Engineering — Adnan Masood (Medium)](https://medium.com/@adnanmasood/agent-harness-engineering-the-rise-of-the-ai-control-plane-938ead884b1d)
- [Top AI Agent Harness Tools and Frameworks 2026 — Atlan](https://atlan.com/know/best-ai-agent-harness-tools-2026/)
- [Claude Code Harness Runtime Architecture 2026 — Pasquale Pillitteri](https://pasqualepillitteri.it/en/news/1892/claude-code-harness-runtime-architecture-2026-guide)

### 学术
- [OctoBench: Benchmarking Scaffold-Aware Instruction Following (arXiv:2601.10343)](https://arxiv.org/abs/2601.10343)
- [AutoHarness: improving LLM agents by automatically synthesizing a code harness (arXiv:2603.03329)](https://arxiv.org/abs/2603.03329)
- [Natural-Language Agent Harnesses (arXiv:2603.25723)](https://arxiv.org/html/2603.25723v1)
- [Building AI Coding Agents for the Terminal: Scaffolding, Harness, Context Engineering (arXiv:2603.05344)](https://arxiv.org/html/2603.05344v1)
- [Dive into Claude Code: The Design Space of Today's and Future AI Agent Systems (arXiv:2604.14228)](https://arxiv.org/html/2604.14228v1)

### 资源汇总
- [awesome-harness-engineering — GitHub](https://github.com/ai-boost/awesome-harness-engineering)
- [HKUDS / OpenHarness — GitHub](https://github.com/HKUDS/OpenHarness)
- [MiniMaxAI / OctoBench Dataset — Hugging Face](https://huggingface.co/datasets/MiniMaxAI/OctoBench)

### DC-Agent 内部文档
- [SYSTEM_ARCHITECTURE.md](../SYSTEM_ARCHITECTURE.md)
- [AGENTS.md](../AGENTS.md)
- [SESSION_HANDOFF.md](../SESSION_HANDOFF.md)
- [harness-engineering-phase1-plan.md](../harness-engineering-phase1-plan.md)
- [UPGRADE_SMOKE_REPORT_2026-05-09.md](../UPGRADE_SMOKE_REPORT_2026-05-09.md)
- [PROJECT_DELIVERY_SUMMARY.md](../PROJECT_DELIVERY_SUMMARY.md)
- [ROUTER_INTENTS_DRAFT.md](../ROUTER_INTENTS_DRAFT.md)
- [HERMES使用说明.md](../HERMES使用说明.md)
- [DOC/Harness马绳任务系统.md](Harness马绳任务系统.md)
- [DOC/记忆系统架构设计.md](记忆系统架构设计.md)
- [DOC/Hermes定位与双系统架构设计.md](Hermes定位与双系统架构设计.md)

---

> **下一步建议**：先开 Phase 0 的 4 个工单进 `TASK_DASHBOARD.md`，并把本路线图入库 `changelogs/`。Phase 1 启动前请先完成 OctoBench-DC checklist v0.1（≥ 50 项），让后续每一个 PR 都能被本路线图所定义的 sensor 度量。
