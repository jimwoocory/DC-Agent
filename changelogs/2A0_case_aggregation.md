# 2A-0 Case 聚合层 v0

**分支**：`feat/2a0-case-aggregation` · **范围**：W0 Plan A 第三件交付

## 做了什么

在 Harness Task 之上叠了一层 **Case** 聚合容器：一个业务流（含甲方沟通、初稿、终稿、评审、修改、Hermes 深挖、定稿等多步）现在可以对应一个 case，挂多个 task、多个交付物、多个角色。

- `astrbot/core/case/`：`contracts.py` / `case_store.py` / `engine.py` / `__init__.py`，aiosqlite sidecar，独立 `data/cases.db`，两表 `cases` + `case_events`（事件流：task_attached / deliverable_added / role_assigned / version_bumped / status_changed / archived）
- `astrbot/builtin_stars/builtin_commands/commands/case.py` + `main.py`：`/case new|context|list|attach|archive|status` 子命令，按 `/task` 范式实现
- `astrbot/core/star/context.py`：新增 `case_engine` / `case_store` / `get_current_case(umo)`
- `astrbot/core/core_lifecycle.py`：在 Harness sidecar 之后初始化 CaseStore/CaseEngine，复用 harness_store 做 task 摘要解析
- `astrbot/core/pipeline/process_stage/router_stage.py`：`_handle_task_intent` 创建 task 后软挂接到当前活跃 case，配置项 `case.auto_attach_task`（默认 True，挂接失败不阻断 Harness）

## 设计原则

- Case 不替代 Task：Task 仍是原子工作单元，Case 是聚合容器
- Case 表与 Harness 表完全隔离（独立 db 文件、独立 schema），未改动 harness.db
- 一个 session 同一时刻只能有一个 active case（`get_active_case_for_session` 仅返回非 terminal）
- archive_hook v0 仅打日志，预留给 G4（NAS 归档）

## 验收

- 新增 49 个测试（store 16 / engine 19 / commands 13 / integration 1）全过
- harness/router 64 个回归全过；core_lifecycle 26 个全过
- ruff check 干净（针对本次改动文件）
- `scripts/smoke_startup_check.py` 通过；启动后 `data/cases.db` 自动建 `cases` + `case_events` 两表

## 不在范围

NAS 归档（G4 / 2A-3）、跨平台 case 聚合、case 间依赖、Hermes Bridge 改动（G2 并行任务）。
