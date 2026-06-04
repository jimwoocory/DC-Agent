# dc_engines.alert_channel

> DC-Agent 全栈告警推送引擎 · 把"通知用户"这件事抹平成单一接口

---

## 核心理念

| 之前 · 各组件自己发告警 | 引擎化之后 |
|---|---|
| 看门狗 `osascript display notification`（只本机） | `dc_engines.alert_channel.send_alert(...)` |
| codex 诊断报告写文件你看不到 | 同上 → 飞书推送给你 |
| LLM 异常静默吞了 | 同上 |
| 飞书机器人挂了你不知道 | 同上 |

**任何想"通知人"的地方都走这里 · 一处升级，全局受益**。

---

## 快速开始

### 1. 配置（已经做完）

`data/config/alert_channel.yaml`：
- 用 **巅池-技术（DevOps）** 作为告警 bot
- 接收人是你（蔡挺，open_id 在巅池-技术 app 下）
- `warning` / `critical` 走 macOS + 飞书；`info` 只走 macOS

### 2. Python 代码里调

```python
from dc_engines.alert_channel import send_alert, send_alert_async

# 同步（脚本里方便）
send_alert(
    title="🚨 hermes_gateway 挂了",
    body="codex 诊断: ...",
    level="critical",  # info / warning / critical
)

# 异步（plugin / FastAPI 里方便）
result = await send_alert_async(
    title="..", body="..", level="warning"
)
print(result.sent_to)    # ['macos', 'lark']
print(result.errors)     # {} 或 {'lark:某人': '...'}
```

### 3. bash / shell 里调

```bash
.venv/bin/python -m dc_engines.alert_channel \
    --title "🚨 服务挂了" \
    --body "诊断报告..." \
    --level critical
```

---

## 配置详解

`data/config/alert_channel.yaml`：

```yaml
lark_app_id: cli_a978167822785bcb   # 巅池-技术（DevOps）作告警 bot

recipients:
  - open_id: ou_xxx                  # 注意：open_id 是【告警 bot app】下的，不是其他 app
    name: 蔡挺
    min_level: warning               # 该接收人只收 warning 及以上的告警

channels_enabled:
  macos: true
  lark: true

level_routing:
  info:     [macos]                  # info 只本机
  warning:  [macos, lark]            # warning 本机 + 飞书
  critical: [macos, lark]            # critical 同上 + fallback 兜底

fallback_to_macos_on_lark_error: true
```

---

## 设计要点

### 1. 凭证不重复维护

`app_secret` 自动从 `data/cmd_config.json` 借 —— 你已经在 AstrBot 里配过巅池-技术 凭证，alert_channel 直接复用。改 secret 只需改一处。

### 2. open_id 必须跟告警 bot 一致

**飞书 open_id 跨 app 不同**。如果用巅池-技术 作告警 bot，接收人 open_id 必须是**他在巅池-技术 app 下的** open_id。

获取方法：让该用户私聊巅池-技术 发任意消息 → 看 `astrbot.log` 里 `[巅池-技术（DevOps）(lark)] ou_xxx/...` 那段。

### 3. 多接收人 + min_level 过滤

未来要加 IT 同事、老板、HR 都行 —— 在 recipients 列表加新条目，按各自 `min_level` 决定收不收。

### 4. 失败兜底

飞书推送失败 → 自动再发一次 macOS（带"飞书推送失败"标记），保证至少本机能看到。

---

## 已接入的调用方

| 调用方 | 何时触发 | level |
|---|---|---|
| `scripts-watchdog/diagnose.sh` | 看门狗探针 ok→fail | critical |
| (未来) OAuth token 健康监控 | token 快过期 / 失效 | warning |
| (未来) LLM 消耗超阈 | 每日消耗 > ¥X | warning |
| (未来) 知识库同步失败 | feishu_sync 连续 N 次失败 | warning |
| (未来) 灰度测试事件 | 异常对话被 silent_observer 捕到 | info |

---

## 调试

### 看推送日志

```bash
# 看门狗的告警推送错误（如果有）
tail -f data/watchdog/incidents/alert_channel.log
```

### 手动测试推送

```bash
# 测试 macOS 通道
.venv/bin/python -m dc_engines.alert_channel --title "测试 info" --body "..." --level info

# 测试飞书 + macOS
.venv/bin/python -m dc_engines.alert_channel --title "测试 warning" --body "..." --level warning

# 测试 critical（双通道 + fallback）
.venv/bin/python -m dc_engines.alert_channel --title "测试 critical" --body "..." --level critical
```

### 加新接收人

直接改 `data/config/alert_channel.yaml` → 下次调用自动生效（不需要重启）。

---

## 未来扩展

| 能力 | 工作量 |
|---|---|
| 加微信通知（企业微信 Bot）| 1-2 小时 |
| 加钉钉通知 | 1-2 小时 |
| 加 SMS（阿里云短信）| 2-3 小时（要充值）|
| 推送到指定群（不是私聊）| 30 分钟 |
| 告警去重 + 频控（30 分钟内同 service 只推 1 次）| 1-2 小时 |

---

## 修订历史

- **2026-05-18**：首版。macOS + 飞书双通道 + diagnose.sh 集成完成。
