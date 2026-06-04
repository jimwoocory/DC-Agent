# DC-Agent 升级计划：基于 Codex Skills 的工程提效

生成时间：2026-06-01  
适用项目：`/Users/dianchi/DC-Agent`  
目标：把已安装的 Codex Skills 纳入 DC-Agent 的日常开发、排障、前端验证、架构治理和交付流程，减少“能跑但不可维护”“改完但没验证”“需求不清就开工”的问题。

## 1. 当前项目判断

DC-Agent 不是单一脚本项目，而是一个多模块系统：

| 模块/目录 | 作用判断 | 升级关注点 |
| --- | --- | --- |
| `astrbot/` | AstrBot 主体能力 | 稳定性、插件兼容、配置安全 |
| `hermes-agent/` | Hermes agent 子系统 | Agent 执行链路、任务状态、依赖边界 |
| `hermes-webui/`、`dashboard/` | Web UI / 管理界面 | 前端体验、浏览器验证、接口契约 |
| `router/`、`dc_engines/` | 路由与业务引擎 | 可测试性、路由解释性、扩展点 |
| `scripts-tools/`、`scripts-watchdog/` | 运维脚本与守护 | 可观测性、回滚、安全执行 |
| `data/`、`hermes-config/`、`logs/` | 运行时数据与配置 | 严禁误提交、敏感信息治理 |
| `tests/`、`harness/` | 测试与评估 | 回归测试、端到端验证 |

## 2. Skills 使用分层

### 2.1 日常高频 Skills

| Skill | 用在 DC-Agent 的场景 | 建议触发方式 |
| --- | --- | --- |
| `diagnose` | 服务异常、日志报错、启动失败、接口不通 | “用 diagnose 排查 DC-Agent 当前问题” |
| `systematic-debugging` | 难复现 bug、链路复杂的故障 | “用 systematic-debugging 找根因” |
| `verification-before-completion` | 改完代码后的收尾验证 | “完成前用 verification-before-completion 检查” |
| `tdd` / `test-driven-development` | 改 `router`、`dc_engines`、核心逻辑前 | “用 tdd 先补测试再改” |
| `triage` | 把需求、bug、反馈整理成可执行任务 | “用 triage 整理这些需求” |
| `to-prd` | 模糊想法转产品需求 | “把这个想法用 to-prd 写成 PRD” |
| `to-issues` | PRD 或计划拆成 issue/任务 | “用 to-issues 拆任务” |
| `playwright` | WebUI/dashboard 浏览器验证 | “用 playwright 验证本地页面” |
| `frontend-design` | WebUI 页面布局和交互优化 | “用 frontend-design 优化 dashboard” |
| `ui-ux-pro-max` | 复杂页面、设计系统、视觉一致性 | “用 ui-ux-pro-max 审一下界面” |

### 2.2 架构与交付 Skills

| Skill | 适用场景 | 产出 |
| --- | --- | --- |
| `improve-codebase-architecture` | 模块职责混乱、重复逻辑、扩展困难 | 架构问题清单、重构优先级 |
| `grill-me` | 开工前拷问方案是否完整 | 问题清单、推荐答案、决策记录 |
| `grill-with-docs` | 结合文档/ADR 审方案 | 文档约束下的方案审查 |
| `writing-plans` | 做较大升级前 | 分阶段执行计划 |
| `executing-plans` | 按计划逐步落地 | 进度记录和执行结果 |
| `finishing-a-development-branch` | 分支收尾、准备提交 | 测试、清理、提交说明 |
| `receiving-code-review` / `requesting-code-review` | PR 前后 | Review 要点和修复清单 |
| `using-git-worktrees` | 多线并行修复或灰度实验 | 隔离工作区 |
| `handoff` | 交接给下一轮 AI 或真人 | 上下文交接文档 |

### 2.3 文档和内容 Skills

| Skill | 可用场景 | 备注 |
| --- | --- | --- |
| `pdf` | 报告、方案、培训材料 PDF 处理 | 适合交付材料 |
| `chart-visualization`、`infographic-creator` | 画架构图、流程图、数据看板说明 | 适合汇报材料 |
| `baoyu-markdown-to-html` | Markdown 转微信排版 | 适合对外发布 |
| `baoyu-translate` | 中英资料互译 | 适合 README/客户材料 |
| `baoyu-infographic` | 长文转信息图 | 适合培训和运营 |

### 2.4 暂不作为 DC-Agent 主力的 Skills

| Skill | 原因 |
| --- | --- |
| `baoyu-cover-image`、`baoyu-xhs-images` | 偏自媒体封面/小红书，不直接服务代码质量 |
| `baoyu-post-to-wechat`、`baoyu-post-to-weibo` | 偏分发，需要账号授权，和项目本体解耦 |
| `caveman` | 偏个人生产力风格，不作为工程流程主链路 |
| `migrate-to-shoehorn`、`scaffold-exercises` | 特定迁移/课程场景，当前不是重点 |
| `gstack` | 体量较大，可作为参考，但不建议直接替代现有 DC-Agent 流程 |
| `use-skill`、`write-a-skill`、`setup-matt-pocock-skills` | 属于技能管理，按需使用 |

## 3. 升级目标

### 3.1 W0：建立标准工作流

目标：让每次 DC-Agent 改动都有“需求澄清、计划、实现、验证、交接”的闭环。

任务：

1. 新建固定提示词模板：
   - 需求不清时使用 `grill-me`。
   - bug 排查时使用 `diagnose` 或 `systematic-debugging`。
   - 功能开发前使用 `to-prd` 和 `to-issues`。
   - 收尾时使用 `verification-before-completion`。
2. 在 `DOC/` 增加一份“Codex 工作流 SOP”。
3. 每个较大改动都记录：
   - 背景
   - 方案
   - 涉及目录
   - 验证命令
   - 回滚方式

验收：

- 任意新任务都能落到一份计划或 issue 清单。
- 不再只凭口头描述直接改核心代码。

### 3.2 W1：排障与验证体系升级

目标：降低生产/本地问题定位成本。

任务：

1. 用 `diagnose` 梳理常见故障：
   - `uv run main.py` 启动失败
   - `start-all.sh` 启动失败
   - WebUI 无法访问
   - 飞书/企业微信回调异常
   - 日志暴涨或配置缺失
2. 把排障路径沉淀到 `DOC/运维排障记录.md` 或单独 SOP。
3. 为高频故障补充最小复现和验证命令。
4. 使用 `verification-before-completion` 作为每次修复收尾检查。

验收：

- 每类故障至少有一个“现象 -> 检查命令 -> 根因定位 -> 修复 -> 验证”的条目。
- 修改后必须能说明跑过哪些验证。

### 3.3 W2：核心模块测试补强

目标：让 `router/`、`dc_engines/`、`harness/` 的关键逻辑可回归。

任务：

1. 用 `tdd` 选择优先测试面：
   - 路由意图识别
   - Hermes/AstrBot 协作契约
   - 飞书卡片构造
   - 命令/脚本封装
   - 配置读取与路径处理
2. 补充单元测试和少量集成测试。
3. 保持项目现有规范：
   - Python 路径处理优先使用 `pathlib.Path`。
   - 提交前运行 `ruff format .`、`ruff check .`。
   - 运行 `make clean-pyc`、`make check-clean`。

验收：

- 新增或修复核心逻辑时，至少有一条相关测试。
- 关键回归命令写入任务交接或 PR 描述。

### 3.4 W3：WebUI/dashboard 体验升级

目标：让管理界面更适合日常运维和展示。

任务：

1. 用 `frontend-design` 审核 dashboard 与 hermes-webui：
   - 信息层级
   - 状态展示
   - 错误反馈
   - 移动端可读性
   - 操作入口一致性
2. 用 `ui-ux-pro-max` 建立轻量设计规则：
   - 颜色、间距、按钮状态
   - 表格/卡片/日志面板布局
   - 空状态、加载态、错误态
3. 用 `playwright` 做浏览器验证：
   - 首页是否加载
   - 核心导航是否可点击
   - 控制台是否有明显错误
   - 移动端布局是否溢出

验收：

- 每个 UI 改动至少有桌面端截图或浏览器验证记录。
- 不引入明显文本溢出、按钮不可点、状态不可见的问题。

### 3.5 W4：架构治理和模块边界

目标：减少 DC-Agent 多模块之间的隐性耦合。

任务：

1. 用 `improve-codebase-architecture` 审查：
   - `router/`
   - `dc_engines/`
   - `hermes-agent/`
   - `scripts-tools/`
2. 识别浅模块、重复适配、跨目录隐式依赖。
3. 产出重构优先级：
   - P0：影响稳定性或安全
   - P1：影响扩展和测试
   - P2：影响可读性但不阻塞
4. 对重大架构决策写 ADR 或补到现有架构文档。

验收：

- 形成一份“架构问题清单 + 优先级 + 推荐改法”。
- 不做无目标大重构，每次只落一个可验证切片。

## 4. 推荐日常提示词

### 4.1 开始一个功能

```text
用 grill-me 拷问我这个 DC-Agent 功能方案。请先阅读相关代码和 DOC，再一次问一个关键问题，并给出你的推荐答案。
```

```text
用 to-prd 把这个想法整理成 DC-Agent 的功能需求，包含目标、非目标、用户流程、接口/配置影响、验收标准。
```

### 4.2 拆任务

```text
用 triage 把这些反馈整理成 bug/enhancement，并标出 needs-info、ready-for-agent、ready-for-human。
```

```text
用 to-issues 把这个 PRD 拆成可执行任务，每个任务包含涉及目录、验证方式和回滚注意事项。
```

### 4.3 排障

```text
用 diagnose 排查 DC-Agent 这个问题。请先看日志、启动脚本、配置，再给出根因假设和验证步骤。
```

```text
用 systematic-debugging 找根因。不要先改代码，先列证据、假设、验证命令。
```

### 4.4 前端

```text
用 frontend-design 审核 dashboard 这个页面，重点看运维可读性、状态表达、操作路径和移动端布局。
```

```text
用 playwright 打开本地 dashboard，验证页面加载、核心操作、控制台错误和移动端布局。
```

### 4.5 收尾

```text
用 verification-before-completion 检查这次 DC-Agent 修改是否完成。请确认测试、lint、运行验证、文档和敏感文件状态。
```

```text
用 handoff 写一份交接，说明已做、未做、验证结果、风险和下一步。
```

## 5. 验证命令清单

按改动范围选择，不要求每次全部执行。

```bash
uv sync
uv run main.py
```

```bash
ruff format .
ruff check .
```

```bash
make clean-pyc
make check-clean
```

```bash
cd dashboard
pnpm install
pnpm dev
```

```bash
cd hermes-webui
pnpm install
pnpm dev
```

```bash
pytest
```

## 6. 风险与约束

1. 不要让 Skills 替代项目事实。所有建议必须回到代码、日志、测试和文档验证。
2. 不要把运行时数据提交到 git：
   - `data/`
   - `logs/`
   - `hermes-config/`
   - token、cookie、数据库、上传文件
3. 不要为“看起来更整洁”做大规模无验证重构。
4. WebUI 修改必须实际打开页面验证，不能只看编译通过。
5. 飞书、企业微信、微博、微信分发类技能涉及账号授权，默认不纳入 DC-Agent 核心升级流程。

## 7. 建议执行顺序

| 周期 | 主题 | 主用 Skills | 主要产出 |
| --- | --- | --- | --- |
| W0 | 工作流标准化 | `grill-me`、`writing-plans`、`handoff` | SOP、任务模板 |
| W1 | 排障体系 | `diagnose`、`systematic-debugging` | 排障手册、验证路径 |
| W2 | 测试补强 | `tdd`、`verification-before-completion` | 核心测试、回归清单 |
| W3 | 前端体验 | `frontend-design`、`ui-ux-pro-max`、`playwright` | UI 改进、浏览器验证 |
| W4 | 架构治理 | `improve-codebase-architecture`、`grill-with-docs` | 架构问题清单、ADR |

## 8. 下一步

优先建议从 W0 和 W1 开始：

1. 先用 `diagnose` 复盘最近一次真实故障或卡点。
2. 再用 `grill-me` 拷问下一项功能方案。
3. 最后把成功流程沉淀成一份固定 SOP。

这样能最快把新装的 Skills 转化成 DC-Agent 的工程收益。
