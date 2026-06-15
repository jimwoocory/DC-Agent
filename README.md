# DC-Agent

DC-Agent 是巅池内部使用的公司级 AI 运营 Agent 系统。它保留 AstrBot 作为多平台聊天运行时，但这个仓库已经不只是 AstrBot 镜像：里面包含了大量独立开发的 DC 模块，用于路由决策、工作流执行、飞书集成、知识导入、记忆治理、看门狗自动化和内部控制台。

本仓库 **不是 AstrBot 上游主项目**。AstrBot 上游仓库在：

- <https://github.com/AstrBotDevs/AstrBot>

## 仓库组成

| 板块 | 作用 | 关键路径 |
| --- | --- | --- |
| 聊天运行时 | 基于 AstrBot 的 IM 运行时、平台适配器、WebUI、模型服务和知识库基础能力 | `astrbot/`, `dashboard/`, `main.py` |
| DC Router | 统一的模型、工作流、业务消息和 DevOps 消息路由决策层 | `dc_router/`, `data/plugins/dc_router/`, `tests/dc_router/` |
| Workflow Harness | 合约驱动的任务状态、评估器、Hermes Bridge、配额闸门和生命周期检查 | `harness/`, `harness/contracts/`, `tests/harness/` |
| DC Engines | 自研业务引擎，包括 case、员工目录、飞书读写、群聊总结、记忆治理、宠物实时系统等 | `dc_engines/` |
| 飞书和 NAS 知识流 | 将员工在飞书共享的文档同步到本地/NAS 知识管线，用于归档和检索 | `nas_sync/`, `scripts-company/` |
| 内部插件 | DC Hub、DC Router、巅池技术日报、飞书频道控制等公司插件 | `data/plugins/` |
| 看门狗和自动化 | 健康检查、launchd/cron/Codex automation 管理、日报推送和故障探针 | `scripts-watchdog/`, `scripts-tools/`, `launchagents/` |
| 文档和计划 | 架构说明、推进计划、变更记录、SOP 和内部方案文档 | `DOC/`, `docs/`, `changelogs/` |

## 核心能力

- 继承 AstrBot 的多平台聊天机器人运行时，并叠加 DC 自有配置和平台集成。
- 通过 `DCRouter` 统一处理业务消息、DevOps 消息、成本信号、安全信号、队列状态和平台上下文。
- 通过 `harness/contracts/` 把功能完成标准写成可验证合约，让 Codex 开发、测试和回归更稳定。
- 自动收集飞书文档，映射员工和部门，沉淀到 NAS、本地知识库和后续记忆治理流程。
- 通过 Hermes Bridge 和 DC Engines 承接深度异步任务、case 聚合、员工洞察、记忆复盘和业务工作流。
- 通过 watchdog 工具管理定时任务、健康日报、暂停/恢复、探针和运维恢复。
- 扩展 Dashboard/WebUI 路由，为内部控制台和运营面板提供入口。

## 快速启动

安装 Python 依赖：

```bash
uv sync
```

启动 API 服务：

```bash
uv run main.py
```

默认服务地址：

```text
http://localhost:6185
```

启动 Dashboard/WebUI：

```bash
cd dashboard
pnpm install
pnpm dev
```

默认 Dashboard 地址：

```text
http://localhost:3000
```

## 常用开发命令

运行窄范围检查：

```bash
scripts/agent-check.sh --profile targeted
```

广泛后端改动或 PR 前运行完整检查：

```bash
scripts/agent-check.sh --profile full
```

格式化和检查 Python 代码：

```bash
ruff format .
ruff check .
```

清理 Python 缓存并检查仓库卫生：

```bash
make clean-pyc
make check-clean
```

运行 DC Router 聚焦测试：

```bash
uv run pytest tests/dc_router -q
```

## Codex 开发流程

改代码前先读：

1. `docs/CODEX_WORKFLOW.md`
2. `harness/contracts/` 中对应功能区域的 contract
3. `tests/` 中最接近的测试

Contract 定义功能完成标准。知识库导入相关工作的第一份 contract 是：

```text
harness/contracts/knowledge_base_import.json
```

任何测试或检查命令结束后，都要立即检查输出；如果有失败或仓库卫生问题，修复后重新运行相关检查。

## 运行数据和敏感信息

不要把运行数据提交到 git。`data/plugins/` 下的代码可以被跟踪；数据库、用户 token、知识库上传文件、日志、`data/temp/` 和 `data/output/` 必须留在本地。

`data/config/` 视为敏感目录，只提交经过审查和脱敏的模板或配置。

## 与 AstrBot 的关系

DC-Agent 起源于 AstrBot，并继续使用 AstrBot 作为核心运行时之一。但本仓库维护的是巅池内部的 DC-Agent 系统，包含大量自研业务模块和运维自动化。仓库首页、README、Issue、徽章和文档链接应以 DC-Agent 为准，不再沿用 AstrBot 上游项目的 README 内容。
