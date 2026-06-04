# llm_router · dc-router 接入指南

AstrBot LLM 智能路由插件，按意图自动选最佳模型。

---

## 当前架构（2026-05-20）

```
飞书消息
  ↓
plugins/llm_router/main.py (LLMRouterPlugin)
  ↓
读 data/config/dc_router_config.json
  ↓
enabled = false ───────────► 走 v1.0 逻辑 (本文件中段)
                                └─ INTENT_TO_PROVIDER / REASONING_PREFIX_PROVIDERS
                                   匹配后调 context.provider_manager.set_provider()

enabled = true ─────────────► 走 dc-router 新路径
                                └─ dc_router_adapter.route_via_dc_router()
                                   └─ router.DCRouter.decide()  (DC-Agent/router/)
                                      ├─ 生图/视频 → gpt_image_plugin / dreamina_plugin
                                      ├─ 有图片/截图/附件 → aihubmix/gemini-3.5-flash 先转文本
                                      │                  → 摘要写回消息后重新 decide()
                                      ├─ 规则不确定 → aihubmix/gemini-3.1-pro-preview JSON 裁判
                                      └─ RouterDecision
                                         ├─ depth=DIRECT  → set_provider
                                         ├─ depth=FRONT   → 已停用（原 Gemini CLI 路径）
                                         └─ depth=HERMES  → QuotaGate + Claude CLI
                                fallback_on_error=true 兜底 → 回 v1.0
```

---

## 开关怎么用

### 当前状态（统一方案）

`data/config/dc_router_config.json`:
```json
{
  "enabled": true,
  "dry_run": false,
  "fallback_on_error": true
}
```

当前统一方案正式接管 business + DevOps + 深度任务 + 多模态 + 媒体生成。

### 打开 dc-router

1. 编辑 `data/config/dc_router_config.json`
2. 改 `"enabled": true`
3. 灰度观察时保持 `"dry_run": true`
4. 真接管时再改 `"dry_run": false`
5. 保存即生效（plugin 每条消息读一次配置，**不需要重启 AstrBot**）

`dry_run=true` 时只写 dc-router 判定日志，实际回复仍走 v1.0。
`dry_run=false` 时 DIRECT / HERMES 才会真正接管。

### 关回去（出问题时）

1. 编辑同一个文件
2. 改 `"enabled": false`
3. 保存——下条消息立即回到旧 v1.0 路径

### 严格模式（出错就停，不 fallback）

把 `fallback_on_error` 设 `false`。**不建议生产用**，员工会感受到错误。

---

## 文件清单

| 文件 | 作用 |
|---|---|
| `main.py` | AstrBot plugin 入口，含 v1.0 全部逻辑 + dc-router 开关 |
| `dc_router_adapter.py` | AstrBot ↔ dc-router 边界层（DIRECT / HERMES CLI 接入） |
| `cli_runner.py` | 受控 subprocess 调用 Claude CLI / Codex CLI（历史上也支持 Gemini CLI，但路由层不再使用） |
| `dc_quota_runtime.py` | QuotaGate lazy 单例（SQLite 队列 + 资源冷却） |
| `README.md` | 本文件 |
| `test_dc_router_path.py` | 单元测试 |

依赖路径（顶层）：
- `/Users/dianchi/DC-Agent/router/` — DCRouter 包（解耦 AstrBot）
- `/Users/dianchi/DC-Agent/harness/` — QuotaGate / QueueStore（解耦 AstrBot）
- `/Users/dianchi/DC-Agent/data/dc_harness.db` — SQLite 队列 + 资源状态（首次 enabled=true 自动建）

---

## 双 router 架构（business / ops）

DCRouter 按 `envelope.metadata['platform_id']` 自动切两套路由表：

- `巅池-技术（DevOps）` → **ops 路由表**（运维场景）
- 其他 platform_id → **business 路由表**（员工业务场景）

两套表完全独立，关键词、前缀、provider 都分开维护。

### Business 路由表（员工业务入口）

| 意图 | 例子 | provider | depth | 接入状态 |
|---|---|---|---|---|
| `casual` | 闲聊 | `cli/antigravity/gemini-3.5-flash` | DIRECT | ✅ agy 主用，失败/排队可切 AIHubMix |
| `ops_writing` | 周报/纪要/邮件 | `aihubmix/gemini-3.5-flash` | DIRECT | ✅ 已接 |
| `multimodal` | 图/语音/附件 | `aihubmix/gemini-3.5-flash` | PREPROCESS | ✅ Flash 转文本后重新路由 |
| `realtime` | 实时热点 | `aihubmix/gemini-3.5-flash` | DIRECT | ✅ 已接 |
| `public_opinion` | 舆情危机 | `aihubmix/grok-4.3` | DIRECT | ✅ 已接 |
| `simple_code` | 报错/小脚本 | `cli/codex/gpt-5.4` | DIRECT | ✅ Codex CLI 直出 |
| `creative` | 营销文案/slogan | `aihubmix/gemini-3.1-pro-preview` | DIRECT | ✅ 已接 |
| `insight` | 用户洞察 | `aihubmix/gemini-3.1-pro-preview` | DIRECT | ✅ 已接 |
| `deep_creative` | 完整营销方案 | `codex/gpt-5.5-high` | DIRECT | ✅ Codex OAuth |
| `deep_insight` | 战略报告 | `codex/gpt-5.5-xhigh` | DIRECT | ✅ Codex OAuth |
| `fallback` | 不确定 | `codex/gpt-5.5-xhigh` | DIRECT | ✅ 已接 |

Business 前缀（强制覆盖）：

| 前缀 | → 意图 |
|---|---|
| `#深度` / `#PRD` | `deep_insight` |
| `#洞察` | `insight` |
| `#创意` | `creative` |
| `#舆情` | `public_opinion` |
| `#代码` | `simple_code` |

### 路由裁判

当 prefix / 附件预处理 / 强关键词都无法确定意图时，新路径会调用：

`aihubmix/gemini-3.1-pro-preview`

它只做分类，不回答用户问题；输出固定 JSON：

```json
{"intent":"ops_writing","confidence":0.91,"reason":"short reason"}
```

如果该 provider 不存在、超时、输出不是 JSON，dc-router 会跳过裁判并进入 fallback，不影响旧路径。

### 多模态预处理

图片、截图、语音、文件附件先进入预处理层：

1. 使用 `aihubmix/gemini-3.5-flash` 做 caption / OCR / 摘要
2. 把 `<attachment_summary>...</attachment_summary>` 写回当前消息
3. 移除直接图片/语音/文件组件，避免目标模型重复处理附件
4. 用合并后的纯文本重新进入同一套 router

因此 `#深度 + 截图` 会先 OCR，再走 `deep_insight → HERMES → Claude CLI`；普通“看图写邮件”会先 OCR，再按关键词走写作模型。

### 媒体生成直达

媒体生成在普通 LLM 路由前拦截：

| 场景 | 主路径 | 兜底 |
|---|---|---|
| 生图 | `gpt_image_plugin` / GPT Image 2 | `dreamina_plugin` 即梦 |
| 静态图 → 动画视频 | `dreamina_plugin` 即梦 | - |
| 文生视频 | `dreamina_plugin` 即梦 | - |

### Ops 路由表（巅池-技术 DevOps 入口）

全用 Codex CLI gpt-5.4（无 Claude/Gemini OAuth），depth 全部 DIRECT。

| 意图 | 例子 | provider | 评级 |
|---|---|---|---|
| `system_status` | "Hermes 状态如何"、"看门狗" | `cli/codex/gpt-5.4` | Codex CLI |
| `queue_status` | "当前队列里几个任务"、"DLQ" | `cli/codex/gpt-5.4` | Codex CLI |
| `quota_gate_view` | "看 aihubmix 用量"、"凭证池" | `cli/codex/gpt-5.4` | Codex CLI |
| `error_debug` | "traceback 看下"、"报错排查" | `cli/codex/gpt-5.4` | Codex CLI |
| `code_ops` | "写个 shell 脚本"、"代码片段" | `cli/codex/gpt-5.4` | Codex CLI |
| `deployment_ops` | "重启服务"、"git push"、"部署" | `cli/codex/gpt-5.4` | Codex CLI |
| `ops_fallback` | 没匹配的运维场景 | `cli/codex/gpt-5.4` | Codex CLI |

Ops 前缀（强制覆盖）：

| 前缀 | → 意图 |
|---|---|
| `#状态` | `system_status` |
| `#队列` | `queue_status` |
| `#配额` | `quota_gate_view` |
| `#排障` | `error_debug` |
| `#部署` | `deployment_ops` |
| `#脚本` | `code_ops` |

**Ops 明确不做的事**：营销 creative / 品牌 insight / 业务 Hermes 深度分析 / 调 Claude OAuth / 调 Gemini OAuth Pro。

### v1.0 前缀（enabled=false 时仍然有效）

`#中` / `#高` / `#超深` / `#深度` / `#实时` / `#写作` 等在 v1.0 路径（enabled=false 时）依然工作。dc-router 启用且 `dry_run=false` 后，`#深度/#PRD/#创意/#洞察/#舆情/#代码` 由新路由接管。

---

## CLI + QuotaGate 机制

dc-router 新路径不直接持有 Gemini OAuth token。闲聊主用 `agy`/Antigravity CLI；失败、排队或员工选择备用时切 AIHubMix。其他 Gemini 相关轻量路由走 AIHubMix provider；深度任务走 Codex OAuth，简单代码和 DevOps 调用 Codex CLI：

```bash
codex exec --model gpt-5.4 --sandbox read-only --output-last-message <file> -
```

安全边界：
- 路由层只恢复 `agy` 的轻量闲聊入口，不让 agy 承担深度分析
- `cli_runner.py` 只允许白名单二进制；Antigravity 使用 `agy`，Codex 使用 `codex`
- 不走 shell，不拼接命令字符串
- Claude 强制只读工具；Codex 强制 `--sandbox read-only` + `--a${REDACTED_API_KEY} never`
- CLI 超时会 kill 进程
- CLI 超时会按进程组 kill，尽量清掉 Claude CLI 子进程，避免僵死残留
- CLI JSON 解析失败 / exit code / timeout 会标记失败，并触发 Codex CLI 快速诊断回传到原会话

排队边界：
- `creative/insight` 已改为 DIRECT：`aihubmix/gemini-3.1-pro-preview`
- `deep_*` 优先使用 Claude CLI 资源（Sonnet/Opus 分流）
- CLI 资源本地短冷却；真正额度由各家服务端控制
- `RUN_NOW` 会先回“任务执行中”，后台完成后 `context.send_message()` 回飞书
- `RUN_NOW` 后如果 Claude CLI 异常/超时/疑似僵死，系统会立即用 Codex CLI 生成故障说明和处理建议发回小助手会话
- `QUEUED` 时不再让员工等队列：取消 pending job，立即切到 `codex/gpt-5.5-xhigh`（`codex-oauth`）直出
- 只有 `codex/gpt-5.5-xhigh` provider 缺失时，才保留排队位置和 watcher 作为最后兜底
- AstrBot 重启后，`enabled=true && dry_run=false` 会启动 pending queue recovery，恢复 SQLite 里的 pending job

---

## 灰度建议

1. 保持默认：
   ```json
   {"enabled": false, "dry_run": true, "fallback_on_error": true}
   ```
2. 观察路由判定：
   ```json
   {"enabled": true, "dry_run": true, "fallback_on_error": true}
   ```
3. 小范围真接管：
   ```json
   {"enabled": true, "dry_run": false, "fallback_on_error": true}
   ```
4. 出问题立即回滚：
   ```json
   {"enabled": false, "dry_run": true, "fallback_on_error": true}
   ```

正式切流前先跑：

```bash
.venv/bin/python data/plugins/llm_router/test_dc_router_path.py
uv run ruff check data/plugins/llm_router router harness
```

---

## 故障排查

### 症状 A: enabled=true 后没反应

检查 plugin 日志，应该看到 `[llm_router] dc-router 调用异常 ...` 或 `[dc-router] intent=...` 这类日志。
没日志说明：
- 配置文件路径不对（重新检查 `data/config/dc_router_config.json` 存在）
- plugin 没重启加载新代码（虽然开关热生效，但 plugin 代码本身改动需重启 AstrBot）

### 症状 B: enabled=true 但员工聊天异常

立即把 enabled 改回 false，员工聊天立即恢复 v1.0。然后看日志定位。

### 症状 C: 想完全卸载 dc-router

1. `enabled=false`（最简单）
2. 删 plugin 目录下 `dc_router_adapter.py` 和 `dc_quota_runtime.py` 也不影响 v1.0（main.py 是 lazy import，没文件就 fallback）
3. 删 `router/` 和 `harness/` 顶层目录也行

### 症状 D: SQLite 数据库要重置

```bash
# 数据库只在 enabled=true 且走到 QuotaGate 时才被用，正常情况删不删都行
rm /Users/dianchi/DC-Agent/data/dc_harness.db
# 下次 plugin 启用时自动重建
```

---

## 历史备份

每次改 `main.py` 前都会自动备份到 `data/_backup_llm_router_main_<TIMESTAMP>.py`。

回滚到任意备份：
```bash
cp data/_backup_llm_router_main_<TS>.py data/plugins/llm_router/main.py
```

---

## 给后续接手者（Codex / 其他 Claude / 人类）

dc-router 当前状态 = **DIRECT / FRONT / HERMES CLI 路径已接，默认关闭，等待灰度**。

下一步要做的事按优先级：
1. 真实飞书灰度：`enabled=true, dry_run=true` 看日志，再切 `dry_run=false`
2. 加 dashboard/命令查看 `dc_harness.db` 队列和资源冷却状态
3. 多模态附件预处理接 `gemini-3.5-flash`
4. 加 Validation Agent（幻觉检测、品牌一致性、法务/GEO 风险）

切记：动 `router/` 和 `harness/` 包内部时，**保持 `DCRouter.decide() -> RouterDecision` 接口稳定**，
这样 plugin 接入面零修改，可以随时换实现（包括换 AI 工具重写）。
