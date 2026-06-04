# DC-Agent Codex Skills 开发计划评估

> **日期**：2026-06-01  
> **评估对象**：[DC-Agent升级计划_CodexSkills_2026-06-01.md](DC-Agent升级计划_CodexSkills_2026-06-01.md)  
> **适用项目**：DC-Agent  
> **结论定位**：Codex Skills 不是主营业务能力本身，而是可提炼为 DC-Agent 的工程治理、排障、分诊、验证和交付能力。

---

## 1. 一句话结论

这些 Codex Skills **不应该被理解为“公司业务插件”**，也不应该直接整体塞进 AstrBot。

它们真正有价值的地方是：把一批成熟的 AI 工程工作方法吸收进 DC-Agent，让系统具备更强的：

- 故障排查能力
- 反馈分诊能力
- 开发任务拆解能力
- 完成前验证能力
- WebUI 验收能力
- 架构治理能力

因此，本计划的正确方向不是“安装 Skills 到业务系统”，而是：

```text
提炼 Skill 方法论
  -> 固化成 SOP / 脚本 / Harness 任务流
  -> 必要时通过 AstrBot 插件提供群聊入口
  -> 最终服务 DC-Agent 自身稳定性和业务交付效率
```

---

## 2. 与公司主营业务的关系判断

### 2.1 直接业务价值：弱

Codex Skills 本身并不天然理解公司主营业务。

它们不会直接解决：

- 客户项目怎么推进
- 老板如何判断方案是否满意
- 员工怎么做日报、周报、项目协作
- 飞书文档、表格、客户资料怎么被业务使用
- 项目交付材料如何归档到知识库
- 部门负责人如何分配任务
- 员工权限、身份和组织上下文如何管理

因此，如果目标是提升公司主营业务，优先级仍然应该放在：

| 主营业务能力 | 当前更应该投入的方向 |
|---|---|
| 飞书资料查询 | 文档/表格白名单、权限、引用来源、答案可信度 |
| 任务提取与提醒 | 群聊任务抽取、负责人、截止时间、状态流转 |
| Case / 项目容器 | 从老板指令到产出物归档的完整闭环 |
| 业务卡片 | 项目卡、任务卡、日报卡、审批卡、满意度卡 |
| 员工身份体系 | open_id、部门、岗位、权限、长期记忆 |
| 老板满意度闭环 | 不满意 -> 深挖 -> 补约束 -> 再产出 |
| 知识库归档 | 项目资料、交付物、复盘记录自动沉淀 |

### 2.2 间接系统价值：强

Codex Skills 对主营业务的帮助主要是间接的：

- DC-Agent 越复杂，越需要可复用的排障流程。
- 插件越多，越需要任务分诊和优先级判断。
- WebUI 越重要，越需要浏览器验收。
- Harness / Hermes / AstrBot 链路越长，越需要完成前验证。
- 多人和多 Agent 协作越频繁，越需要交接和审计记录。

所以它们更像是“造机器、修机器、验机器”的能力，而不是“直接跑业务”的能力。

---

## 3. 是否应该通过 AstrBot 插件实现

### 3.1 不建议一开始做成 AstrBot 插件

原因：

1. **AstrBot 是运行时入口，不是开发治理中心。**  
   Codex Skills 的核心场景是开发、排障、验证、交付，天然更靠近仓库、脚本、测试和 CI。

2. **直接让插件执行工程命令风险高。**  
   例如重启服务、读日志、跑测试、改文件、写配置，都需要权限、审计、白名单和回滚策略。

3. **Skill 本身不是稳定 API。**  
   它更像工作流说明，不适合被系统当成强依赖运行时接口。

4. **业务用户不需要感知 Skill 名称。**  
   员工要的是“帮我排查”“整理这些反馈”“检查任务是否完成”，而不是知道 `diagnose` 或 `verification-before-completion`。

### 3.2 AstrBot 插件适合作为后期入口

当 SOP、脚本、Harness 任务流稳定后，可以做一个轻量插件作为入口，例如：

```text
用户在飞书/QQ 发起命令
  -> AstrBot 插件识别意图并收集上下文
  -> Harness 创建工程任务
  -> 后台 worker 执行受控命令或调用 Codex/Claude
  -> 结果以卡片形式回传
  -> 关键结论写入 DOC 或任务记录
```

插件只负责：

- 收集问题
- 创建任务
- 展示进度
- 回传结果
- 做权限和审批入口

插件不应该直接负责：

- 任意执行 shell 命令
- 任意修改代码
- 任意读取敏感配置
- 自动提交代码
- 自动重启生产服务

---

## 4. 值得吸收的 Skill 能力评估

### 4.1 P0：`diagnose` / `systematic-debugging`

**建议吸收为：DC-Agent 故障排查助手。**

适用场景：

- `uv run main.py` 启动失败
- `start-all.sh` 启动失败
- Dashboard 打不开
- Hermes gateway 异常
- 飞书回调失败
- 插件未加载
- 日志暴涨
- 配置缺失

建议产品形态：

```text
/diagnose dashboard打不开
/diagnose hermes没回群
/diagnose 飞书回调失败
```

后台执行逻辑：

1. 收集现象。
2. 读取白名单日志。
3. 检查端口、进程、配置文件存在性。
4. 输出根因假设。
5. 给出验证命令。
6. 必要时生成修复建议。
7. 将案例沉淀到 [运维排障记录.md](运维排障记录.md)。

价值判断：

| 维度 | 评价 |
|---|---|
| 业务相关性 | 中，保障业务系统可用 |
| 工程价值 | 高 |
| 落地难度 | 中 |
| 风险 | 中，需要命令白名单和敏感信息脱敏 |
| 优先级 | P0 |

---

### 4.2 P0：`verification-before-completion`

**建议吸收为：任务完成前质量闸门。**

适用场景：

- Codex / Claude / Hermes 声称任务完成前
- PR 提交前
- 插件上线前
- Dashboard 改动交付前
- 运维修复后

可固化为：

```bash
make verify-agent-change
make check-clean
```

建议检查项：

- 是否只改了任务相关文件。
- 是否误带 `data/`、`logs/`、数据库、token、cookie。
- 是否跑过相关测试。
- Python 是否跑过 `ruff format .` 和 `ruff check .`。
- WebUI 是否做过浏览器验证。
- 是否记录验证命令和结果。
- 是否有回滚方案。

价值判断：

| 维度 | 评价 |
|---|---|
| 业务相关性 | 中，减少线上事故 |
| 工程价值 | 极高 |
| 落地难度 | 低 |
| 风险 | 低 |
| 优先级 | P0 |

---

### 4.3 P1：`triage` / `to-issues`

**建议吸收为：反馈分诊和任务拆解能力。**

适用场景：

- 老板一次性提出多个问题。
- 员工在群里集中反馈体验。
- 灰度测试后需要整理问题。
- 需求文档需要拆成可执行任务。

建议输出结构：

| 字段 | 含义 |
|---|---|
| 类型 | bug / enhancement / task / question |
| 优先级 | P0 / P1 / P2 |
| 状态 | needs-info / ready-for-agent / ready-for-human |
| 涉及模块 | plugin / dashboard / router / dc_engines / ops |
| 验证方式 | 测试、日志、截图、命令 |
| 回滚注意 | 配置回滚、代码回滚、数据清理 |

与 DC-Agent 业务能力的结合：

- 可以接入 Harness task。
- 可以把飞书群反馈整理成项目任务。
- 可以把灰度测试反馈自动变成待办。
- 可以把老板指令拆成 Case 子任务。

价值判断：

| 维度 | 评价 |
|---|---|
| 业务相关性 | 高 |
| 工程价值 | 高 |
| 落地难度 | 中 |
| 风险 | 中，容易误判优先级 |
| 优先级 | P1 |

---

### 4.4 P1：`tdd` / `test-driven-development`

**建议吸收为：核心模块变更规范。**

适用目录：

- `router/`
- `dc_engines/`
- `harness/`
- `data/plugins/llm_router/`
- 飞书卡片构造相关模块
- 任务提取相关模块

推荐做法：

1. 新功能先写最小失败测试。
2. 修 bug 先写复现测试。
3. 实现后只跑相关测试。
4. 收尾前再跑更大范围回归。

不建议做成普通用户可见功能。它应该是开发规范、Codex SOP 和 CI 规则的一部分。

价值判断：

| 维度 | 评价 |
|---|---|
| 业务相关性 | 中 |
| 工程价值 | 高 |
| 落地难度 | 中 |
| 风险 | 低 |
| 优先级 | P1 |

---

### 4.5 P1：`frontend-design` / `playwright`

**建议吸收为：WebUI 验收流程。**

适用范围：

- `dashboard/`
- `hermes-webui/`
- 系统入口页
- 插件管理页
- Cron / watchdog / 任务状态页
- 飞书业务卡片预览页

建议验收项：

- 首页是否能加载。
- 核心导航是否可点击。
- 控制台是否有错误。
- 移动端是否溢出。
- 表格、日志、卡片是否可读。
- loading / empty / error 状态是否存在。
- 操作入口是否一致。

可产品化方向：

```text
Dashboard 改动
  -> 自动启动 dev server
  -> Playwright 打开核心页面
  -> 记录截图和 console error
  -> 生成验收摘要
```

价值判断：

| 维度 | 评价 |
|---|---|
| 业务相关性 | 中 |
| 工程价值 | 高 |
| 落地难度 | 中 |
| 风险 | 低 |
| 优先级 | P1 |

---

### 4.6 P2：`improve-codebase-architecture`

**建议吸收为：阶段性架构体检。**

适用范围：

- 插件边界
- Router 和业务引擎边界
- Hermes / Harness / AstrBot 协作契约
- 配置读取和路径处理
- runtime data 与可追踪代码的边界
- 脚本和服务之间的契约

建议每月或每个大版本做一次，不建议日常频繁触发。

输出：

| 优先级 | 判断标准 |
|---|---|
| P0 | 影响稳定性、安全或数据正确性 |
| P1 | 影响扩展、测试或模块边界 |
| P2 | 影响可读性但不阻塞 |

价值判断：

| 维度 | 评价 |
|---|---|
| 业务相关性 | 低到中 |
| 工程价值 | 中到高 |
| 落地难度 | 中 |
| 风险 | 中，容易诱发无目标重构 |
| 优先级 | P2 |

---

## 5. 不建议吸收为系统能力的 Skills

以下 Skills 不建议进入 DC-Agent 主升级路径：

| Skill | 原因 |
|---|---|
| `baoyu-cover-image` | 偏内容封面，不是 DC-Agent 核心业务 |
| `baoyu-xhs-images` | 偏小红书运营，不适合作为系统主能力 |
| `baoyu-post-to-wechat` | 依赖账号授权，和工程系统解耦 |
| `baoyu-post-to-weibo` | 同上 |
| `caveman` | 个人沟通风格，不是系统能力 |
| `migrate-to-shoehorn` | TypeScript 特定迁移，当前不适用 |
| `scaffold-exercises` | 教学场景，不适用 |
| `use-skill` / `write-a-skill` | Skill 管理工具，只按需使用 |

这些可以保留为个人工具，不纳入 DC-Agent 产品路线。

---

## 6. 推荐目标架构

### 6.1 分层设计

```text
用户入口层
  - 飞书/QQ 命令
  - Dashboard 按钮
  - CLI / Makefile

编排层
  - AstrBot 插件只负责收集上下文和展示结果
  - Harness 负责创建任务、状态流转、审计记录
  - Hermes / Codex / Claude 负责深度执行或分析

执行层
  - scripts-tools/
  - scripts-watchdog/
  - pytest / ruff / pnpm / playwright
  - 受控日志读取和配置检查

记录层
  - DOC/运维排障记录.md
  - Harness task state
  - Dashboard 验收记录
  - PR / handoff 文档
```

### 6.2 推荐新增内部概念

可以增加一个轻量的“工程任务运行记录”概念，不一定马上建数据库，初期可先用 Harness 任务结构承载。

建议字段：

| 字段 | 说明 |
|---|---|
| `run_id` | 本次工程任务 ID |
| `intent` | diagnose / triage / verify / ui_qa / architecture_review |
| `requester` | 发起人 |
| `source` | 飞书 / QQ / dashboard / CLI |
| `scope` | 涉及目录或服务 |
| `evidence` | 日志、命令输出、截图 |
| `decision` | 判断和建议 |
| `actions` | 已执行动作 |
| `verification` | 验证命令和结果 |
| `risk` | 剩余风险 |
| `status` | pending / running / blocked / done |

---

## 7. 分阶段开发计划

### W0：文档和流程固化

目标：先把 Skill 能力转成 DC-Agent 可执行工作规范。

任务：

1. 保留并完善 [Codex工作流SOP_2026-06-01.md](Codex工作流SOP_2026-06-01.md)。
2. 为排障、分诊、验证各补一个固定模板。
3. 明确哪些任务可以由 Agent 做，哪些必须人工确认。
4. 在 `AGENTS.md` 中引用关键 SOP。

验收：

- 新任务能根据 SOP 判断使用哪类流程。
- 文档中明确“插件不是直接执行危险操作的主体”。
- 有完成前 checklist。

优先级：P0

---

### W1：排障助手最小闭环

目标：把 `diagnose` 能力转成 DC-Agent 可复用排障流程。

任务：

1. 梳理 5 类高频故障：
   - AstrBot 启动失败
   - Dashboard 打不开
   - Hermes 没有回群
   - 飞书回调异常
   - 插件未加载或配置缺失
2. 为每类故障定义只读检查命令。
3. 产出 `scripts-tools/diagnose-*.sh` 或统一 `diagnose.sh`。
4. 将检查结果整理进 [运维排障记录.md](运维排障记录.md)。
5. 后续再决定是否接 AstrBot 命令入口。

验收：

- 每类故障至少有“现象 -> 检查命令 -> 根因假设 -> 验证方式”。
- 检查脚本默认只读，不修改配置、不重启服务。
- 敏感内容不会出现在输出里。

优先级：P0

---

### W2：完成前质量闸门

目标：把 `verification-before-completion` 固化成命令和 checklist。

任务：

1. 增加或完善 `make check-clean`。
2. 增加可选 `make verify-agent-change`。
3. 检查 runtime 文件、备份文件、敏感配置。
4. 输出明确的通过/失败原因。
5. 将结果接入 PR 或 handoff 模板。

验收：

- 能发现常见 runtime 文件误提交。
- 能提示 `data/config/` 敏感变更。
- 能列出建议验证命令。

优先级：P0

---

### W3：反馈分诊和任务拆解

目标：把 `triage` / `to-issues` 转成业务反馈处理能力。

任务：

1. 定义反馈分类 schema。
2. 将灰度测试反馈、老板指令、员工建议统一转成任务。
3. 与 Harness task 对接。
4. Dashboard 展示任务状态。
5. 后续通过飞书卡片支持人工确认优先级。

验收：

- 一段混合反馈能拆成结构化任务。
- 每个任务包含类型、优先级、负责人建议、验证方式。
- 人工可以修改分类和优先级。

优先级：P1

---

### W4：WebUI 验收流程

目标：把 `frontend-design` / `playwright` 转成 dashboard 改动验收流程。

任务：

1. 定义 dashboard 核心页面清单。
2. 写 Playwright smoke test。
3. 检查 console error。
4. 覆盖桌面和移动端视口。
5. 保存关键截图或验收摘要。

验收：

- Dashboard 首页能被自动打开。
- 核心导航能点击。
- 移动端不出现明显横向溢出。
- 控制台无明显运行错误。

优先级：P1

---

### W5：AstrBot 插件入口

目标：在工程流程稳定后，给业务用户一个自然入口。

建议插件名称：

- `engineering_ops_plugin`
- `devops_skill_plugin`
- `codex_workflow_plugin`

建议命令：

```text
/diagnose <问题描述>
/triage <反馈文本>
/verify <任务ID>
/handoff <任务ID>
```

插件职责：

- 收集上下文。
- 做权限判断。
- 创建 Harness 任务。
- 展示进度卡片。
- 回传最终结果。

不做：

- 任意 shell 执行器。
- 自动改代码。
- 自动提交。
- 直接读取敏感配置。
- 直接重启生产服务。

验收：

- 群里能发起排障任务。
- 用户能看到任务状态。
- 所有命令都有审计记录。
- 危险操作需要人工确认。

优先级：P2

---

## 8. 风险评估

| 风险 | 说明 | 缓解方式 |
|---|---|---|
| 误把 Skill 当业务能力 | 导致投入偏离主营业务 | 明确它们主要是工程治理能力 |
| 插件直接执行危险命令 | 可能误删数据、泄露配置、重启服务 | 只读优先、命令白名单、人工审批 |
| 过度自动化 | Agent 误判完成、误判优先级 | 引入 verification gate 和人工确认 |
| 文档先行但不落地 | SOP 写完没人用 | 配套 Makefile/scripts/Harness 状态 |
| 无目标架构重构 | 消耗时间且风险高 | 每次只做一个可验证切片 |
| 敏感信息进入记录 | token、cookie、数据库路径泄露 | 输出脱敏、敏感目录检查 |
| 和主营业务抢资源 | 影响飞书业务流、任务系统进度 | P0 只做排障和验证，插件入口后置 |

---

## 9. 推荐优先级

### 必做

1. `verification-before-completion` -> 完成前质量闸门。
2. `diagnose` -> 只读排障助手。
3. `triage` -> 灰度反馈和老板指令分诊。

### 应做

4. `playwright` -> Dashboard smoke test。
5. `tdd` -> 核心模块测试规范。

### 阶段性做

6. `improve-codebase-architecture` -> 月度架构体检。

### 暂不做

7. 内容分发、封面图、小红书、微博、微信发布类 Skills。

---

## 10. 最终建议

如果资源有限，不建议把“Codex Skills 系统升级”作为独立大项目。

更合理的方式是把它拆进现有 DC-Agent 路线：

```text
业务主线：
飞书资料查询、任务提取、Case 归档、业务卡片、知识库

工程保障线：
diagnose、verification、triage、playwright、tdd
```

近期最值得落地的不是 AstrBot 插件，而是两个低风险高收益切片：

1. **只读排障 SOP + 脚本**  
   先解决 DC-Agent 多服务、多插件、多配置带来的定位成本。

2. **完成前质量闸门**  
   防止 Agent 或人类改完代码后漏测、误提交 runtime 数据、遗漏敏感配置。

等这两件稳定后，再把它们接入 Harness，最后才通过 AstrBot 插件开放给飞书/QQ 群使用。

一句话：

> Codex Skills 不直接升级公司主营业务，但能显著升级 DC-Agent 的“自我维护能力”。它们应该作为工程保障层被吸收，而不是作为业务插件被照搬。
