# W3 / 2A-3 Cowork Prompt Draft — 飞书资料读+写（含 G4 归档闭环）

> **目的**：W2 合并后立即派给 cowork。W3 是 Codex 时间表 05-25 ~ 05-31 启动的关键模块，覆盖飞书文档/表格查询 + Case 归档写回 KB 双向能力。
>
> **派发命令**：`spawn_task(title=..., tldr=..., prompt=<下方内容>, isolation="worktree")`

---

## Chip Title

`W3 / 2A-3: 飞书资料读+写（白名单查询 + Case 归档入 KB / G4）`

## Chip TL;DR

群里 `@DC-Agent 查 <资料名>` 触发——从白名单飞书文档/表格/客户资料里检索回答，每条带来源链接。同时实装 `/case archive` 真闭环：把 Case 的所有 deliverables 打包写入 NAS 命名空间触发 watchdog 自动入 KB。覆盖 Codex 问卷 82%（飞书文档读取）+ 73%（飞书表格读取）双需求 + 老总指定 case 流程 Step 9 归档闭环。

---

## Prompt（完整）

```text
你是一个在 DC-Agent 项目（/Users/dianchi/DC-Agent）开发的 Claude Code 远程 agent。本次任务是 W3 / 2A-3：飞书资料读+写（含 G4 归档闭环）。

## 业务背景

W3 是 2A 阶段第三大件，**覆盖两条独立但互补的能力**：

### 读：飞书白名单资料查询（Codex 问卷 82% / 73%）

员工常需"翻查上次客户方案"、"找应标文件模板"、"调用历史项目资料"。痛点是这些资料散落在飞书文档、飞书表格、NAS 客户资料文件夹里，每次得人工翻。

### 写：Case 归档闭环（G4，case Step 9）

老总指定 case 流程 Step 9："定稿回档进 KB"。W0 2A-0 已建 Case 聚合层但 archive_hook v0 只打日志。本任务把 hook 实装成：归档时把 case 的全部 deliverables 按规范打包到 NAS 命名空间 → watchdog 自动入 KB。

参考文档：
- [DOC/DC-Agent二期统一方案_2026-05.md](../../DOC/DC-Agent二期统一方案_2026-05.md) §四 W3 行
- [DOC/Case业务流落地评估] 见统一方案 §3 G4
- [SYSTEM_ARCHITECTURE.md](../../SYSTEM_ARCHITECTURE.md) §知识库 + §NAS sync
- [deliverables/DC-Agent二期需求对比报告.pdf](../../deliverables/DC-Agent二期需求对比报告.pdf) 表二

## 你要做的事

## Part 1 / 读：飞书白名单资料查询

### 1.1 白名单配置

新增 `astrbot/core/feishu_reader/whitelist.yaml`：

```yaml
documents:  # 飞书云文档
  - id: doccnXXXXX
    name: "营销策划模板库"
    domain: "marketing"
    description: "历史营销方案模板"

tables:  # 飞书多维表格
  - id: bascnXXXXX
    name: "客户应标资料"
    domain: "client"
    description: "应标文件、合同模板"
    primary_key: "client_name"

folders:  # NAS 客户资料映射（已有 4 路 KB 之一）
  - kb_id: "client_archives"
    name: "客户档案"
    domain: "client"
```

允许配置在 `data/feishu_whitelist.yaml`（被本任务的 .gitignore 例外白名单化）；启动时合并 yaml 默认值 + data/ 覆盖值。

### 1.2 飞书 API 读取

新增 `astrbot/core/feishu_reader/`：

- `client.py`：复用项目内已有的 lark-oapi (>=1.4.15) 认证。先 grep 现有 `lark` 用法定位 token 配置位置；不要新建独立的 auth
- `document_reader.py`：`async def read_doc(doc_id: str) -> DocContent`，返回结构化内容（含标题、段落、表格 blocks）
- `table_reader.py`：`async def read_table(table_id: str, *, filter: dict | None = None, limit: int = 100) -> list[TableRow]`
- `contracts.py`：`DocContent` / `TableRow` 数据模型

错误处理：`UnauthorizedError`（凭证失效）、`NotInWhitelistError`、`FeishuAPIError`（带 HTTP code）。

### 1.3 触发与意图

群里 `@DC-Agent` + 关键词：
- `@DC-Agent 查 <关键词>`
- `@DC-Agent 找 <关键词>`
- `@DC-Agent 资料 <关键词>`
- `@DC-Agent 翻一下 <关键词>`

`astrbot/core/router_config.yaml` 加 task_intent：intent_type=resource_query，category=skill，confidence 0.95+。
`tests/eval/router_bench_v0.yaml` 加 4-6 个对应样本，bench 仍 ≥ 95%。

### 1.4 查询管道

新增 `astrbot/core/feishu_reader/query_engine.py`：

```python
async def query_resources(
    keyword: str,
    *,
    whitelist: Whitelist,
    feishu_client,
    nas_kb_manager,
    domain_hint: str | None = None,  # 当前 Case domain 可作为权重提示
    limit: int = 5,
) -> list[QueryHit]:
    ...
```

策略：
1. 关键词分别投到 doc / table / nas-kb 三路检索
2. 每路前 N 个候选
3. 用 BM25 + 语义相似度（aihubmix embedding 已就绪）综合排序
4. 返回前 limit 个

输出格式（回群 markdown）：

```
## 找到 N 条相关资料：

1. **{title}** ({domain})
   {summary}
   [打开](飞书链接 / KB 引用)
   匹配字段：{matched_field}

2. ...
```

### 1.5 Case 关联

若当前 session 有 active case：把这次查询作为 case event 写入（不当 deliverable，因为是引用不是新产物）。`event_type="resource_query_logged"`，payload 含 keyword + hits 元数据。

---

## Part 2 / 写：Case 归档闭环（G4）

### 2.1 `/case archive` 真闭环

修改 `astrbot/builtin_stars/builtin_commands/commands/case.py` 的 archive 命令逻辑：

1. 当前 case 必须有 ≥1 deliverable（防空归档）
2. 调用新增的 `archive_to_nas(case, deliverables)` → 落到 NAS
3. NAS watchdog 自动入 KB（无需新代码，watchdog 已存在）
4. 写 case_event `archived_to_nas` 记录路径 + KB ID
5. 把 case status 置 `archived`

新增 `astrbot/core/case/archiver.py`：

```python
async def archive_to_nas(
    case: Case,
    deliverables: list[Deliverable],
    *,
    nas_root: Path,
    kb_mapping: dict[str, str],  # case.domain → KB id
) -> ArchiveResult:
    """
    生成 NAS 路径：{nas_root}/cases/{YYYY-MM}/{case_id_short}_{slug(case.name)}/
    内容：
      - manifest.json: case 元信息 + deliverables 索引
      - deliverables/: 实际文件（按 kind 子文件夹：text/image/video/ppt）
      - history.md: case_events 全流水生成的归档摘要
    返回 ArchiveResult { nas_path, kb_id, ingested_files: int }
    """
    ...
```

### 2.2 archive_hook 接入

`astrbot/core/case/engine.py` 中 `archive_case()` 调用 hook，把 v0 的 logger.info 替换为：

```python
result = await self._archive_hook(case, deliverables)
await self.store.append_event(case.case_id, "archived_to_nas", asdict(result))
```

`_archive_hook` 是 `Callable[[Case, list[Deliverable]], Awaitable[ArchiveResult]]`，在 lifecycle 初始化时注入 `archive_to_nas`（仿 memory_promoter 注入模式）。

### 2.3 NAS 路径与命名

约定（与现有 `nas_sync/config.yaml` 协调）：
- NAS 挂载点参考 SYSTEM_ARCHITECTURE.md 中 `192.168.1.35` SMB
- cases 归档目录：`<nas_root>/dc-agent-cases/{YYYY-MM}/`
- 每个 case 一个独立子目录：`{case_id[:8]}_{slug(case.name)[:40]}/`

slug 规则：
- 中文保留
- 空格 → `-`
- 去掉 `/` `\` `:` `*` `?` `"` `<` `>` `|`

### 2.4 manifest.json schema

```json
{
  "case_id": "...",
  "name": "...",
  "client_name": "...",
  "status": "archived",
  "platform_id": "qq",
  "version": 3,
  "created_at": "...",
  "archived_at": "...",
  "roles": {"业务": "...", "设计": "...", ...},
  "task_ids": ["...", "..."],
  "deliverables": [
    {"kind": "text", "path": "deliverables/text/proposal_v3.md", "version": 3, "created_at": "..."},
    {"kind": "image", "path": "deliverables/image/poster_v2.png", "version": 2, "created_at": "..."}
  ],
  "history_md_path": "history.md"
}
```

### 2.5 离线场景容错

NAS 不可达时（CIFS/SMB 挂载失败、网络断、磁盘满）：
- 不要 raise 把 archive_case 整个搞挂
- 把归档对象写到 `data/case_archive_dlq.jsonl`（仿 hermes_dlq_logger）等后续重试
- case status 仍变 archived（业务侧已"完事"），但 case_event 多一条 `archive_failed`
- 加 ops 工具脚本 `scripts/retry_case_archive_dlq.py` 手工跑

## Part 3 / 共同部分

### 3.1 测试

新增（**Phase 2 才写**）：
- `tests/unit/test_feishu_whitelist.py` — yaml 加载、合并、白名单决策
- `tests/unit/test_feishu_document_reader.py` — mock lark-oapi，6-8 case
- `tests/unit/test_feishu_table_reader.py` — mock，6-8 case
- `tests/unit/test_feishu_query_engine.py` — 三路 rank 合并、domain hint 权重、limit
- `tests/unit/test_case_archiver.py` — slug 生成、manifest 结构、NAS 路径生成、DLQ 容错（≥ 12 case）
- `tests/integration/test_case_archive_e2e.py` — `/case archive` 端到端：建 case → 加 deliverables → archive → 验证 NAS 路径 + manifest + event 记录
- `tests/integration/test_resource_query_flow.py` — 端到端：@DC-Agent 查 → mock feishu → 回 markdown
- 共计目标 ≥ 40 测试

### 3.2 router_config.yaml + bench

router_config.yaml 加 resource_query 规则；router_bench_v0.yaml 加 4-6 个样本。bench 仍 ≥ 95%。

### 3.3 Case 触发协议（连通 W1 / W2）

W3 完成后，**Case 业务流就完整了**：
- W1 group_summary 写 case 的某种 deliverable
- W2 task_extractor 抽出任务挂到 case
- W3 资源查询事件写入 case_events
- W3 `/case archive` 把全套 deliverables 打包入 KB

确保以上四件互不冲突：
- 不修改 W0 G2 / 2A-0 / W1 group_summary / W2 task_extractor 的代码（必要时只读）
- 新增模块按 namespace 隔离：`astrbot/core/feishu_reader/` + `astrbot/core/case/archiver.py`（注意 archiver 放 case/ 子目录是 OK 的，不冲突 case_store/engine.py）

## 关键参考文件

- `astrbot/core/group_summary/` — W1 LLM-JSON + Router skill 范式（v0 后已合并）
- `astrbot/core/task_extractor/` — W2 抽取范式（看是否已合并，参考其 case 软挂接代码）
- `astrbot/core/case/engine.py` + `case_store.py` — Case 聚合层（不改，复用 archive_hook 注入点）
- `astrbot/core/harness/memory_promotion.py` — memory_promoter 注入范式（archive_hook 仿这个）
- `astrbot/core/hermes_callback_dispatcher.py` + `hermes_dlq_logger.py` — DLQ 范式
- `astrbot/core/kb/` 或类似 — 已有 KB API，**先 grep 找到再调用**
- `nas_sync/watcher.py` + `config.yaml` — 现有 NAS watcher 配置（如何映射到 KB id）
- `pyproject.toml` deps: `lark-oapi>=1.4.15` 已就绪

## Phase 1（必做）

**先做 Phase 1**：模块骨架 + 接口签名 + ≥ 5 个设计偏差候选，让我 review。等 "继续 Phase 2"。

Phase 1 重点确认：
- 飞书 API auth/token 复用现有哪个配置入口（不要新建）
- KB write API 长什么样（grep 找）
- NAS 挂载点 / 路径约定（看 SYSTEM_ARCHITECTURE + nas_sync/config.yaml）
- whitelist 配置存哪儿（yaml in repo / data 下 / 数据库）
- DLQ 复用 hermes_dlq_logger 还是独立一份

## 验收（合并前）

1. 所有新增 ≥ 40 测试通过
2. 现有 master 测试全过（合并前跑 `pytest tests/ -q`）
3. ruff check 干净
4. `scripts/smoke_startup_check.py` 通过
5. router bench v0 + 新增样本 ≥ 95%
6. 启动后 `data/feishu_whitelist.yaml`（如选 yaml 方案）能正常加载
7. `/case archive` 在 mock NAS 路径下能跑通端到端流程
8. NAS 不可达时归档进 `data/case_archive_dlq.jsonl`，不抛 unhandled exception
9. 提交 `changelogs/2A3_feishu_read_write.md`

## 工作流

- git 分支：`feat/2a3-feishu-read-write`
- commit message：`feat(skills+case): add feishu reader + case archiver (W3 / 2A-3)`
- **Phase 1 → 等 review → Phase 2**
- 完成后回报：测试通过情况、commit hash、文件清单、有无遗留、ops 部署清单（飞书凭证位置、NAS 挂载状态确认）

## 不要做的事

- 不要直接全量同步飞书空间（性能 + 权限风险）—— 严格按 whitelist
- 不要在飞书 reader 里写新的 LLM 调用（这是检索，不是生成）
- 不要让 archive_hook 阻塞 archive_case 的状态变更（NAS 失败时 case 仍变 archived）
- 不要默认开启对 NAS 实际写盘的集成测试（用 tmp_path 或 mock）
- 不要把 lark-oapi 升级到最新版本（pyproject.toml 已锁 >=1.4.15，本任务保持兼容）
- 不要在 v0 引入飞书 OAuth 流（用现有 token-based auth）
```

---

## 派发清单（W2 合并后用）

```python
mcp__ccd_session__spawn_task(
    title="W3 / 2A-3: 飞书资料读+写（白名单查询 + Case 归档入 KB / G4）",
    tldr="...",  # 见本文档顶部
    prompt=<本文档 ## Prompt 节>,
    isolation="worktree",
)
```

---

## 与 W0/W1/W2 的衔接

- **W0 2A-0**：复用 Case engine + archive_hook 注入点
- **W0 G2**：复用 hermes_dlq_logger 模式 → case_archive_dlq
- **W1 2A-1**：复用 router skill 路由模式
- **W2 2A-2**：完成后 case 业务流完整闭环（总结 + 任务 + 查询 + 归档）

## 风险与未知

- 飞书 API 权限：白名单文档/表格可能需要单独授权 bot 应用，ops 需要操作
- NAS 挂载稳定性：CIFS/SMB 在容器场景常断，DLQ 必须工作
- KB 写入速度：watchdog 可能延迟数秒，归档"成功"信号要明确（写入 NAS 完成 = 成功，不等待 KB 索引完成）
- 飞书内容更新：白名单文档内容随时改，检索结果可能"过时"，v0 不做缓存失效逻辑

## 部署清单（W3 合并后 ops 操作）

1. 在飞书开放平台给 bot 应用授权目标文档/表格的读权限
2. 把飞书 token/secret 配在 cmd_config.json（位置由 Phase 1 确定）
3. 编辑 `data/feishu_whitelist.yaml` 加入实际 doc_id / table_id
4. 确认 NAS 挂载点存在 `dc-agent-cases/` 目录（不存在则手动创建）
5. 重启 AstrBot 验证启动日志显示"feishu_reader initialized, N whitelist entries"
