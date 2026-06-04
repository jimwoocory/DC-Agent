# 巅池-技术 日报 plugin

把 DC-Agent 变成自带"AI 系统维护员"。每天自动：

1. **01:00 北京（= 美西 PT 10:00）** — `searcher.py` 优先调 agy / Antigravity
   抓硅谷四大 AI 实验室（OpenAI / Anthropic / Google Gemini / xAI Grok）当日动态 → `raw_news.md`
2. **紧接着** — `analyzer.py` 调 agy / Antigravity 读 raw_news，做三件事：
   - 资讯解读（按对蔡挺/巅池的意义重排）
   - 当日学习笔记（挑一个 AstrBot / Hermes / dc_engines 模块深入读）
   - DC-Agent 只读巡检（git / watchdog / cron 日志）
   - 产出 `report.md`
3. **09:00 北京** — Python reporter 把 report.md：
   - 飞书 interactive card 私聊给蔡挺（巅池-技术（DevOps）发）
   - 写飞书 wiki『日常任务报告』空间子页
   - 同步 Mac 桌面 + NAS（→ AstrBot nas_knowledge KB 自动 ingest）
   - 写 `delivery.json`

第二个任务（自学+巡检）**严格只读**——prompt 里硬约束不动代码。

## 架构

```
                ┌──────────────────────────┐
   01:00 BJT ─→ │ dianchi-tech-night       │
                │   阶段A: searcher.py     │  → agy / Antigravity
                │           → raw_news.md  │
                │   阶段B: analyzer.py     │  → agy / Antigravity → report.md
                └──────────────────────────┘
                                                           │
                                                           ▼
                ┌──────────────────────────┐    ┌──────────────────┐
   09:00 BJT ─→ │ dianchi-tech-report      │ → │ reporter.py      │
                │   (shell wrapper)        │    │  ├ feishu IM    │ → 蔡挺
                └──────────────────────────┘    │  ├ wiki 子页    │ → 日常任务报告 空间
                                                 │  ├ Mac 桌面     │ → ~/Desktop/日常任务报告/
                                                 │  └ NAS inbox    │ → AstrBot KB auto-ingest
                                                 └──────────────────┘

   dashboard 看历史 ──→ /api/plug/dianchi_tech/recent  (本 plugin 提供)
```

调度走 LaunchAgent（不是 cron），用户级 launchd 跑在 GUI session 上下文，能 access keychain
（agy / 飞书凭证都在 keychain 里）。

## 文件清单

| 文件 | 用途 |
|---|---|
| `prompts/agy_analyze.md` | agy 的分析+学习+巡检 prompt |
| `analyzer.py` | 阶段 B：调用 agy 生成 `report.md`，登记 `learning_log.json` |
| `searcher.py` | 阶段 A：优先 agy 抓新闻，aihubmix grounding 兜底 |
| `agy_runner.py` | PTY 包装 `agy --print`，给阶段 A/B 共用 |
| `reporter.py` | 09:00 推送（飞书 IM + wiki + 桌面 + NAS） |
| `main.py` | AstrBot plugin，暴露 `/api/plug/dianchi_tech/{recent,report/<date>,health}` |
| `_conf_schema.json` | 配置：蔡挺 union_id、wiki 空间名 |
| `../../../scripts-tools/dianchi-tech-night.sh` | 夜间任务入口 |
| `../../../scripts-tools/dianchi-tech-report.sh` | 汇报任务入口 |
| `~/Library/LaunchAgents/io.dianchi.tech.{night,report}.plist` | LaunchAgent 触发器 |

## 部署

### 0. 准备飞书 wiki 空间（**必做一次**）

飞书 API 限制：创建 wiki 空间只允许 `user_access_token`，机器人的 `tenant_access_token` 没权限自动建。
所以一次性手动建好：

1. 飞书 → 知识库 → 「新建知识库」 → 名字填 **`DC-Agent 运维`**（与默认值一致，否则改 plugin 配置）
2. 设置 → 成员管理 → 添加 **巅池-Agent小助手** → 编辑权限
3. 之后机器人就能自动建子页

### 1. 配置蔡挺 open_id

在 AstrBot dashboard → plugins → dianchi_tech → 配置：
- `cai_ting_open_id`: 蔡挺真实 open_id（form 形如 `ou_xxxxxxxxx...`）
- `wiki_space_name`: 默认『DC-Agent 运维』

或直接写 `data/config/dianchi_tech_config.json`：

```json
{
  "cai_ting_open_id": "ou_129de...",
  "wiki_space_name": "DC-Agent 运维",
  "data_root": "/Users/dianchi/DC-Agent/data/dianchi_tech"
}
```

> plugin 启动时会从 dashboard 配置回写一份到这里，让 cron（不在 plugin 进程内）读得到。

### 2. 装 cron

```bash
./scripts-tools/install-dianchi-tech-cron.sh install
./scripts-tools/install-dianchi-tech-cron.sh status   # 看是否装上
```

### 3. 手动 dry-run（先验证两个阶段都能跑通）

```bash
# 立即跑一次夜间任务（不用等 01:00）
./scripts-tools/dianchi-tech-night.sh

# 跑完看产出
ls -la data/dianchi_tech/$(date +%Y-%m-%d)/
cat data/dianchi_tech/$(date +%Y-%m-%d)/report.md

# 立即跑一次汇报
./scripts-tools/dianchi-tech-report.sh

# 看推送结果
cat data/dianchi_tech/$(date +%Y-%m-%d)/delivery.json
```

## 飞书凭证依赖

reporter 通过 `dc_engines.feishu_hub.get_client()` 拿单例 client，凭证来自
`data/feishu_whitelist.yaml` 或 `nas_sync/config.yaml` 的 `feishu.app_id/app_secret`。
凭证缺失时 reporter 不抛异常，但会退非 0 + 在 `delivery.json` 标失败。

需要的飞书应用权限：
- `im:message`（发私聊）
- `wiki:wiki`（建/列 wiki 空间）
- `wiki:wiki:write`（建 wiki 节点 + 移动 docx 进 wiki）
- `docs:document`（创建/写 docx）

## 一个月回顾 checklist（2026-06-21）

- [ ] 飞书私聊是否每天 09:30 准时到？
- [ ] wiki 空间是否自动建好？子页累计 ~30 篇？
- [ ] 学习笔记轮换是否合理（没重复学同一模块超过 3 次）？
- [ ] 资讯解读是否真的有"对我们的意义"洞察，还是流水账？
- [ ] DC-Agent 巡检有没有发现过真实异常？
- [ ] cron.log 有没有反复出现的失败模式（API 限流 / 超时 / 凭证）？
- [ ] 决策：是否升级到"AI 可以提 PR"模式？

## 后续可加（先不做）

- 学习笔记导出累计 PDF
- agy 巡检发现异常 → 触发 Hermes 主动告警
- 资讯 + 学习内容做向量索引（喂 feishu_reader 增强问答）
- 周报 / 月报汇总
