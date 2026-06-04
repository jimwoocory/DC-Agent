# Codex 任务：帮 dianchi_tech 日报解决 agy 调用问题

日期：2026-05-21
求助人：Claude

---

## 1. 我要做的事

用户让我做一个 cron 任务：每天给『巅池-技术（DevOps）』bot 安排两件事：

- **01:00 BJT**（= 美西 PT 10:00）抓硅谷四大 AI 厂商当日动态（OpenAI / Anthropic / Gemini / xAI Grok）
- 接着用 Claude CLI 做分析 + 学 AstrBot/Hermes 文档 + DC-Agent 只读巡检 → 产 `report.md`
- **09:00 BJT** 把 `report.md` 飞书私聊蔡挺（union_id `on_d02f744ffca7d68eac1afee00d7edb71`）+ 写进『DC-Agent 运维』wiki

骨架我都搭好了：
- `data/plugins/dianchi_tech/{main.py, reporter.py, prompts/claude_analyze.md, _conf_schema.json, README.md}`
- `scripts-tools/{dianchi-tech-night.sh, dianchi-tech-report.sh, install-dianchi-tech-cron.sh}`
- `~/.dc-agent.env` 已存好 `DIANCHI_TECH_APP_SECRET`
- 飞书 IM 链路已实测 200 OK（用 union_id）

**唯一卡点：阶段 A 的"抓新闻"用 agy 在 cron 里调不通。**

---

## 2. agy 卡点

`scripts-tools/dianchi-tech-night.sh` 阶段 A 跑 `agy -p "<prompt>"`，结果：

```
Authentication required. Please visit the URL to log in: https://accounts.google.com/...
Waiting for authentication (timeout 30s)...
Error: authentication timed out.
```

我已经查到的根因：
- agy token 存在 `~/.gemini/oauth_creds.json`，`expiry_date` 在今早 07:56 BJT 就过期
- `agy -p`（headless print 模式）**不会**自动用 refresh_token 续期，直接弹 OAuth URL
- 只有 `agy -i`（交互模式）会续期
- 跟从哪个进程起子进程无关（cron / shell / AstrBot 都一样），是 agy `-p` 自己的设计
- 你之前在 `data/plugins/llm_router/cli_runner.py:97 run_antigravity()` 用 15s timeout 短任务能成，是因为那时候 token 还没过期

---

## 3. 需要你做的事

**让 agy 能在 cron 脚本里被稳定调用。** 任选最干净的一条：

### 方案 A：写个 agy token 刷新守护
- 在 cron 触发 night.sh 之前（比如 00:55）跑一个 `refresh_agy_token.py`
- 用 `~/.gemini/oauth_creds.json` 里的 `refresh_token` 调 Google OAuth `/token` endpoint 换新 access_token，回写文件
- 之后 `agy -p` 看见有效 token 就能直接用

### 方案 B：agy-bridge 本地服务
- 你之前提过的"本地 agy 交互桥接服务"——常驻持有一个 `agy -i` PTY 会话
- 暴露 `http://127.0.0.1:xxxx/ask` 接口
- night.sh 阶段 A 改成 `curl 127.0.0.1:xxxx/ask -d '{"prompt":"..."}'`
- 配套 launchd plist 自启 + watchdog 探针

### 方案 C：判断 agy 不可救药，直接换通道
- 阶段 A 改用 Tavily（key 已在 `cmd_config.json provider_settings.websearch_tavily_key[0]`）抓新闻
- 再用 aihubmix gemini-3-flash-preview（key `sk-IQOgv...`）整理成 markdown
- 完全脱离 agy

**你判断哪个最对，做完告诉我**。然后我跑端到端验证。

---

## 4. 给你的现状一览

| 文件 | 状态 |
|---|---|
| `data/plugins/dianchi_tech/main.py` | 已就绪 |
| `data/plugins/dianchi_tech/reporter.py` | IM 200 OK；wiki 代码已写未跑通 |
| `data/plugins/dianchi_tech/prompts/agy_search.md` | 给 agy 的 prompt，方案 C 下作废 |
| `data/plugins/dianchi_tech/prompts/claude_analyze.md` | Claude 分析 prompt，质量已验证 |
| `scripts-tools/dianchi-tech-night.sh` | 阶段 A 跑 agy 失败；阶段 B Claude 部分 OK |
| `scripts-tools/dianchi-tech-report.sh` | 跑通；只是 cron 时间 09:30 待改 09:00 |
| `data/dianchi_tech/cron.log` | 现场日志，看这里 |

**别动**：飞书 sender（必须是 巅池-技术 DevOps，不是 Agent小助手）、reporter.py 的 IM 部分、Claude prompt。

---

## 7. 追加需求：小助手排队卡片与等待动效

（用户后续追加的另一个需求，跟上面 cron 任务无关，独立做即可）
