# Codex 修复任务：DC-Agent 工程质量与可维护性整改方案

> **生成时间**：2026-05-22  
> **执行对象**：Codex CLI  
> **来源**：Grok 4.3 工程评审（2026-05-22）  
> **预估总工时**：18~24h（分 3 个 Phase，可拆分多次交付）  
> **当前分支状态**：建议基于 `master` 新建 `refactor/engineering-hygiene-202505` 分支执行

---

## 0. 背景与目标

DC-Agent 在过去 1 个多月围绕「AstrBot + Harness + Hermes + Case」构建了双系统架构，并在真实业务 case（老总指定流程）上实现了 W0/W1 阶段的功能闭环。

**当前核心问题**（Grok 评审结论）：
- 仓库卫生严重缺失，大量构建产物与备份文件污染 git
- 自定义核心（Router / Harness）体量偏薄，对 AstrBot 内部机制依赖过重
- 插件数量多且碎片化，职责边界模糊
- 关键路径测试覆盖不足
- 三层架构（AstrBot 前台 / Harness 中台 / Hermes 执行）的协作契约缺乏显式文档

**本次整改目标**：
1. 把仓库恢复到「干净、可长期维护」状态
2. 显著提升 Router 与 Harness 核心的可测试性与可演进性
3. 明确三层边界，减少未来技术债
4. 为二期（2A-2B）后续重度开发打下工程基础

**严禁行为**：不允许在本次任务中进行大范围业务功能新增或「顺手重构」。

---

## 1. 工作原则（必须严格遵守）

1. **先诊断，后修改**  
   每处理一个 Task 前，必须先执行 `git status` + `git diff -- <关键文件>`，确认问题依然存在。

2. **严格范围控制**  
   只做本文件明确描述的事项。发现额外问题请追加到文末「附录：评审遗留问题」，不要自行修改。

3. **每项完成后必须验证**  
   - 运行对应测试（见各 Task）
   - 在本文件对应位置打 `[x]` 并记录 commit hash（先不 push）
   - 重要变更需附带简短 rationale

4. **禁止污染工作区**  
   任何涉及 secret、config 的修改，必须确保不会把生产值带入 git。

5. **变更节奏**  
   Phase 0（仓库卫生）必须作为独立 commit 完成，且建议单独 PR。后续 Phase 可分批。

---

## 2. 任务清单

### 🔴 Phase 0：仓库卫生与工程基础（最高优先级，建议 1 天内完成）

#### Task E-1：彻底清理构建产物与备份文件污染

**问题描述**：
- `router/`、`harness/`、`data/plugins/` 下存在大量 `.pyc` 文件及 `__pycache__/` 目录
- 多处存在 `_backup_*.py`、`.bak`、`.py.bak` 文件（如 `feishu_card_streamer/` 下多达 4 个备份）
- `dc_engines/` 下同样存在大量 pyc 和备份

**修复要求**：
1. 确认 `.gitignore` 已正确包含以下规则（当前已有部分，需补全）：
   ```gitignore
   **/__pycache__/
   **/*.py[cod]
   **/*$py.class
   **/*.so
   **/.pytest_cache/
   **/_backup_*.py
   **/*.bak
   **/*.py.bak
   **/*_backup_*.py
   **/tmp/
   **/output/
   **/logs/
   ```
2. 从工作区和 index 中移除所有 pyc、__pycache__、备份文件（使用 `git rm --cached` + 物理删除）。
3. 运行 `git status` 确认不再出现上述文件。
4. 在根目录新增 `scripts/clean_pyc.sh`（或 Makefile 目标），方便后续任何人一键清理。

**验证命令**：
```bash
find . -name "*.pyc" -o -name "__pycache__" -o -name "_backup_*.py" -o -name "*.bak" | grep -v ".git" | head -20
# 期望：仅剩 .git 下的历史记录（如果有），工作区应为空
git status --porcelain | grep -E '\.pyc|__pycache__|_backup_|\.bak' || echo "✅ 清理干净"
```

**完成标记**：- [ ] E-1 done（commit: ）

---

#### Task E-2：处理运行时数据目录污染问题

**问题描述**：
- `data/` 目录下混杂了大量运行时生成内容（数据库、上传的知识库文件、临时图片、日志等），当前 `.gitignore` 里虽然有 `data`，但实际仍有部分被追踪或被误提交。

**修复要求**：
1. 审计当前被 git 追踪的 `data/` 下内容，区分「应该版本控制的配置」和「运行时产物」。
2. 建议策略：
   - `data/plugins/*/main.py` 等代码继续保留
   - `data/*.db`、`data/knowledge_base/`、`data/temp/`、`data/output/` 等明确加入 `.gitignore`
   - 必要时把少量示例配置迁到 `config/examples/` 下
3. 更新 `DOC/系统架构总览.md` 中的「关键数据存储」表格，明确哪些是 git 管理的。

**验证**：
```bash
git ls-files | grep "^data/" | head -30
# 应只剩少量必要文件
```

**完成标记**：- [ ] E-2 done（commit: ）

---

#### Task E-3：补充 .gitignore 并建立「干净仓库」检查机制

**修复要求**：
1. 完善根 `.gitignore`（参考上面 E-1 建议 + 现有内容）。
2. 在 `Makefile` 或 `scripts/` 下增加 `make check-clean` 目标，CI/本地开发前可快速检查。
3. 在 `CONTRIBUTING.md` 或 `AGENTS.md` 中增加「提交前必须执行的清理步骤」说明。

**完成标记**：- [ ] E-3 done（commit: ）

---

### 🔴 Phase 1：核心模块可测试性与边界加固（建议 3-4 天）

#### Task E-4：Router 核心路径补测试

**涉及范围**：`router/` 整个目录 + `data/plugins/llm_router/`

**修复要求**：
1. 为 `router/classifier.py`、`router/rules.py`、`router/decision.py`、`router/taxonomy.py` 建立单元测试（使用 pytest）。
2. 重点覆盖：
   - 前缀规则（`#深度`、`#PRD` 等）匹配
   - 关键词规则冲突时的优先级（PUBLIC_OPINION > DEEP_* > 普通）
   - LLM fallback classifier 的 mock 路径
   - `RouterIntent` 枚举完整性
3. 测试文件建议放在 `router/tests/` 或复用 `data/plugins/llm_router/` 下的测试目录。
4. 在 `router/README.md`（若无则新建）中说明如何单独运行 router 测试。

**验收标准**：
- 新增至少 12 个有意义的测试用例
- `pytest router/ -q --cov=router` 覆盖率 ≥ 65%（核心模块）

**完成标记**：- [ ] E-4 done（commit: ）

---

#### Task E-5：Harness 核心状态机与生命周期补测试

**涉及范围**：`harness/` + `dc_engines/dc_engines/harness/`

**修复要求**：
1. 针对 `harness/task_state.py`、`harness/queue_store.py`、`harness/quota_gate.py` 补强测试。
2. 重点场景：
   - QuotaGate 准入决策（RUN_NOW vs QUEUED）
   - 资源 cooldown 生效与释放
   - HarnessTask 状态流转合法性（PENDING → RUNNING → COMPLETED/FAILED）
   - 内存提升（memory_promotion）触发条件
3. 确保 `dc_engines/tests/test_harness_lifecycle.py` 已覆盖的核心逻辑在本次整改后仍然通过，且新增边界用例。

**验收标准**：
- 关键状态转换路径有显式测试断言
- 新增至少 8 个测试用例

**完成标记**：- [ ] E-5 done（commit: ）

---

#### Task E-6：产出《AstrBot / Harness / Hermes 三层协作契约》文档

**位置**：`DOC/AstrBot-Harness-Hermes-协作契约_v1.md`（新建）

**必须包含内容**：
- 每层的单一职责定义（用表格）
- 允许的跨层调用白名单（目前实际存在的注入点必须显式列出）
- 禁止的跨层行为（例如直接修改 AstrBot 内部 Stage、绕过 Harness 直接调用 Hermes）
- 状态同步策略（Case / Task / Hermes session 的映射关系）
- 升级与演进路径（未来要把哪些逻辑从 AstrBot 里抽出来）
- 变更影响评估 checklist

**验收标准**：
- 文档被 `DOC/系统架构总览.md` 引用
- 后续任何跨层改动必须先更新此契约

**完成标记**：- [ ] E-6 done（commit: ）

---

### 🟡 Phase 2：架构收敛与长期可维护性（建议 4-5 天，可与二期功能并行）

#### Task E-7：插件碎片化治理（第一批）

**目标**：把当前 20+ 自定义插件做一次分类与收敛试点。

**修复要求**：
1. 产出 `DOC/DC-Agent插件现状与收敛方案_2026-05.md`（分类 + 依赖关系图 + 建议合并列表）。
2. 选择 2-3 组明显重叠的插件进行试点合并（例如多个 feishu_* 插件、harness 相关 sensor 插件）。
3. 合并后必须保持对外 CLI 和卡片行为兼容（通过测试或灰度验证）。

**完成标记**：- [ ] E-7 done（commit: ）

---

#### Task E-8：降低对 AstrBot 内部机制的直接依赖（可行性评估 + 小步改造）

**问题**：
当前大量业务逻辑通过 monkey patch、context 注入、`InternalAgentSubStage` 回调等方式与 AstrBot 深度耦合。

**本轮要求**（不做大重构）：
1. 梳理所有「直接操作 AstrBot 内部对象」的位置，列出 top 10 高风险点（文件 + 行号 + 风险描述）。
2. 选择其中风险最高、改动最小的一处，完成一次「抽薄」改造（把业务逻辑迁到 dc_engines 里，通过事件/回调解耦）。
3. 在 Task E-6 的契约文档中增加「解耦路线图」一节。

**完成标记**：- [ ] E-8 done（commit: ）

---

#### Task E-9：建立「干净提交」与「架构守护」机制

**具体动作**：
1. 在 pre-commit 配置中增加「禁止提交 pyc / backup 文件」的检查（如果已有则强化）。
2. 在 AGENTS.md 中增加「Codex 及人类开发者提交 checklist」（包含本次整改的所有关键原则）。
3. 新建 `scripts/validate_architecture.py`（占位），未来可逐步加入三层边界静态检查。

**完成标记**：- [ ] E-9 done（commit: ）

---

## 3. 交付物清单（必须全部产出）

- [ ] Phase 0 完成后，仓库 `git status --porcelain` 接近干净（仅允许少量未追踪的运行时文件）
- [ ] `DOC/AstrBot-Harness-Hermes-协作契约_v1.md`
- [ ] `DOC/DC-Agent插件现状与收敛方案_2026-05.md`
- [ ] Router + Harness 核心新增测试文件 + 覆盖率报告
- [ ] 本文件所有 Task 完成标记 + 对应 commit hash 列表
- [ ] 最终的整改总结（可追加在本文件末尾或单独一篇）

---

## 4. 验收方式

1. Codex 完成所有 🔴 任务后，通知 dianchi 进行人工复核。
2. 关键验证命令需在 PR description 或本次任务文件末尾贴出执行结果截图/日志。
3. 建议整改 PR 至少拆成 2 个：
   - `refactor: 仓库卫生与 .gitignore 强化`（Phase 0）
   - `refactor: 核心可测试性与架构契约`（Phase 1）

---

## 附录：评审遗留问题（Codex 发现后请追加）

（本区域供 Codex 在执行过程中记录额外发现的问题，格式建议：`[文件路径] 问题简述 + 建议优先级`）

---

**执行前请先回复**：  
「已阅读完整整改方案，理解工作原则，将从 Phase 0 开始执行。」

之后再开始实际修改。