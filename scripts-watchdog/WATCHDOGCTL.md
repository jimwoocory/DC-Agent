# DC-Agent 任务控制面

`watchdogctl` 是 DC-Agent 后台任务的统一控制入口。它保留原有
launchd、crontab、Codex automation 和 `dc-watchdog.sh` 控制能力，并通过
只读 Task Control Plane 汇总 Knowledge Cycle、AstrBot Cron 和 Harness 任务。

Codex 在统一视图中固定标记为 `advanced_executor`。它可以承担受限的复杂
诊断、深度分析和生成任务，但不拥有调度或运行时控制权。

## Codex 重要工具

飞书小助手中发送 `Codex 高级工具` 可打开确定性的能力卡。填写深度分析表单、
预览和返回修改都不会调用模型；只有点击“确认并开始执行”后，才会生成专用
`#codex工具` 标记并进入真实的 Codex CLI Adapter。

该 Adapter 固定使用 `--sandbox read-only`，可以读取项目并完成分析与复核，不能
修改项目文件或接管调度。每次执行都会在 Harness 中创建
`advanced_executor:deep_reasoning` 任务，记录开始、完成/失败、模型、耗时、用量和
prompt 哈希；不会保存完整 prompt、完整回答或 CLI 命令。

本机项目开发使用独立的 `project_engineering` 能力，只允许
`local_operator` 显式调用。运行时强制模型为 `gpt-5.6-sol`，推理强度最低
`high`，可用档位为 `high`/`xhigh`/`max`，默认 `max`，并使用
`--sandbox workspace-write`。模型、推理强度或调用
权限不符合时，会在启动 Codex CLI 子进程前拒绝。该写入能力不对飞书开放，也不
拥有任何自动任务的调度权。

原有 `#codex`、`#codex高`、`#codex超深` 仍是模型选择前缀，不等同于 Codex
重要工具，也没有被改写语义。

## Watchdog 自动修复边界

`dc-watchdog.sh` 仍负责每分钟确定性探活，进程生命周期仍由 launchd KeepAlive
负责。故障生成 incident 后，`diagnose.sh` 只调用一次 OpenAI Codex
`gpt-5.6-sol`（`max` 推理、只读沙盒、临时会话），在同一个严格 JSON bundle
中生成诊断报告和修复建议，再由本地代码拆分。模型只能选择
`observe_only`、`restart_managed_service` 或
`manual_intervention`，不能提供要执行的命令。

`repair_engine.py` 会重新校验 incident、服务、置信度和风险，只把明确登记的
进程/监听故障映射到固定的 `scripts-tools/safe_restart.sh` 命令。代码、配置、
权限、依赖、数据和未知根因一律停在人工处理，不允许模型直接修改。每个 incident
最多执行一次；每个服务每小时最多尝试 2 次，连续 2 次验证失败后熔断 6 小时。
执行前会再次检查原始 probe，若 launchd 已恢复服务则取消动作且不消耗重试预算；
执行后还必须复查同一个 probe。未来若加入持久化修复动作，未登记固定回滚命令时
会被策略引擎拒绝。

NAS 助手健康探针与本地重启敏感探针使用同一个 15 分钟宽限窗口，单次短暂 502
不会立即触发 Codex 诊断。若诊断已经开始，确定性修复引擎也会在创建人工审核前
复查原始 probe；服务已恢复时直接记录 `service_already_recovered`，不再留下
`pending` 审核和后续升级提醒。

`watchdogctl.sh status watchdog --json` 的 `repair` 区域会只读展示策略参数、
各服务剩余尝试次数、连续验证失败、熔断剩余秒数和最近修复结果。相同信息会投影
为 `watchdog_repair` 任务并显示在 Dashboard 的“Watchdog 自愈策略”和“最近自愈
结果”区域。这里不提供重启、解除熔断或重放 incident 按钮，不能绕过策略引擎。

`manual_intervention` 建议会进入“人工介入队列”，初始状态为 `pending`。接单和
确认解决必须先生成绑定 incident、操作、当前状态和审核记录指纹的 120 秒 Control
Plan，再确认同一个 `plan_id`：

```bash
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh plan-review <incident_id> acknowledge --json
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh review <incident_id> acknowledge --confirm-plan <plan_id>
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh plan-review <incident_id> resolve --json
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh review <incident_id> resolve --confirm-plan <plan_id>
```

状态只能按 `pending → acknowledged → resolved` 前进。审核操作只更新 review
元数据，不执行修复命令、不解除熔断，也不消耗或恢复自动修复预算。

人工介入通知继续走统一 `dc_engines.alert_channel`。首条 incident 告警会附带
“打开 Dashboard 审核”链接；该按钮只是 HTTP(S) 导航，不包含卡片回调和修复动作。
每分钟 watchdog 会异步运行 `review_notifier.py`，按固定阶段提醒：新建待审核、
15 分钟未接单、60 分钟未接单升级、接单后 4 小时未解决。只在至少一个告警渠道
发送成功后记录该阶段；发送失败不会改 review 状态，下分钟自动重试。可通过
`DC_AGENT_DASHBOARD_URL` 设置通知中的 Dashboard 地址。

同一健康循环还会异步运行 `repair_analytics.py`。脚本自身按 5 分钟间隔限流，只读
聚合 `repair_state.json`、`repairs.jsonl` 和 `review_notifications.jsonl`，原子生成
`repair_analytics.json`。快照包含 24 小时/7 天自动修复次数和成功率、已验证成功
修复的平均/P95 MTTR、人工审核 SLA、当前熔断和脱敏的 Codex 深巡检建议。
`watchdogctl` 和 Dashboard 的“自愈可靠性指标”只展示这个派生快照；生成指标不会
调用 Codex，也不会改变修复、审核或通知状态。现有 Codex App 两小时深度巡检可把
该快照作为稳定输入，但仍由 App 的 cron 决定何时唤醒。

审计保留仅提供预览：repair/notification 事件和已解决审核按 90 天、incident
状态按 30 天统计超期候选数，`files_deleted` 永远为 `0`。本阶段没有删除、截断或
压缩实现；若未来需要清理历史，必须另外设计绑定准确文件、范围和回滚策略的
Control Plan。

## 常用命令

查看状态：

```bash
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh status nas
```

查看完整统一任务记录：

```bash
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh status all --json
```

暂停或恢复分组必须先生成 Control Plan，再在 120 秒内确认同一个
operation-specific `plan_id`：

```bash
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh plan-pause nas --json
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh pause nas --confirm-plan <plan_id>
```

```bash
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh plan-resume nas --json
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh resume nas --confirm-plan <plan_id>
```

恢复计划只包含当前未运行且 authoritative 的任务；暂停计划只包含当前运行中
且可控的任务。两者都会显示 schedule 和执行身份。superseded、只读或状态不匹配
的任务只会出现在 `skipped`。确认前如果 operation、任务状态或执行身份发生变化，
旧 `plan_id` 会被拒绝。

`crontab:dc-watchdog` 是保护性 Deterministic Controller，不进入任何分组暂停
计划。如确需停止，必须先生成精确的单任务 Control Plan。该计划会明确提示：
停止它将同时停止统一探活、告警和 Knowledge Cycle 每分钟调度。

```bash
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh plan-pause-one cron dc-watchdog --json
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh pause-one cron dc-watchdog --confirm-plan <plan_id>
```

单任务计划的哈希绑定 `scope=item`、任务类型、任务 key、operation 和执行身份；
分组计划、其他任务计划、过期计划或状态变化前生成的计划都不能用于本操作。

暂停或恢复单个任务：

```bash
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh pause-one launchd dianchi-tech-night
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh resume-one launchd dianchi-tech-night
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh resume-one cron dc-watchdog
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh pause-one codex nas
```

退役已登记且有明确替代者的旧调度入口：

```bash
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh retire-one cron astrbot-http-watchdog
```

`retire-one` 只接受登记了 `replacement_task_ids` 的 superseded 任务，不能
用于删除仍然 authoritative 的调度任务。

Codex automation 只允许暂停，不能通过 `resume` 或 `resume-one` 恢复为调度器。

支持的分组：

- `all`：全部已登记任务
- `nas`：NAS / 飞书同步、相关夜间日报、NAS workflow heartbeat
- `night`：夜间生成和上午推送类任务
- `sync`：文件同步类任务
- `watchdog`：看门狗和告警类任务
- `dianchi-tech`：巅池技术日报相关任务
- `onboarding`：入职问卷轮询任务

## 当前控制范围

launchd：

- `io.dianchi.tech.night`
- `io.dianchi.tech.report`
- `com.dcagent.baidu-nas-sync`
- `com.dcagent.feishu-sync`
- `com.dcagent.nas-watchdog`

crontab：

- `DC-Agent watchdog`
- `DC-Agent 巅池-技术 日报`
- `DC-Agent 问卷→入职卡 轮询`

Codex automation：

- `~/.codex/automations/nas/automation.toml`
- `~/.codex/automations/nas-workflow/automation.toml`

`dc-watchdog.sh` 探针：

- `nas_watchdog_heartbeat`
- `feishu_sync_heartbeat`

## 统一只读范围

- 上述 launchd、crontab、Codex automation 和探针
- `data/watchdog/knowledge_cycle_state.json`
- AstrBot `cron_jobs`
- Harness `harness_tasks`

统一只读范围不会扩张 pause/resume 权限；尚未迁移的来源只显示状态。

## 已收拢的调度职责

旧 `~/.local/bin/astrbot_watchdog.sh` 同时探活并自行启动 AstrBot，和正式
运行链存在重复。它的职责由以下 authoritative 任务接管：

- `launchd:astrbot-runtime`：AstrBot 进程生命周期（KeepAlive）
- `watchdog_probe:astrbot_api`：AstrBot HTTP 探活

控制面会把旧入口显示为 `migration_required` 或 `retired`，不会再把它视为
Deterministic Controller。

飞书/NAS 自动同步的 Authoritative Sync Scope 是
`knowledge_cycle:feishu_nas_workflow` 管理的飞书云目标队列。旧
`com.dcagent.feishu-sync` 的全文件夹 launchd 轮询和 Knowledge Cycle 内的
`feishu_repair` 自动调度均已退役；底层 `nas_sync/feishu_sync.py` 仍保留给
主工作流的 targeted 调用和人工按需执行。现有 workflow pause marker 不受影响。

## 注意

`pause` 不会删除脚本、plist、日志或 automation 文件，只会停用、卸载、移除
crontab 入口，或把 Codex automation 标记为 `PAUSED`。

Dashboard 的“暂停本组”和“恢复本组”使用同一 Control Plan Interface：先展示
准确清单，用户确认后才执行。普通单任务 `pause-one` / `resume-one` 仍可直接执行；
标记为 `critical` 的暂停操作必须使用精确的单任务 Control Plan。

`watchdogctl` 是受保护的控制 Seam。直接执行 `scripts-watchdog/install-cron.sh
remove`、直接修改 crontab 或在 Python 内部调用底层 Implementation 属于显式的
operator-level 操作，不经过 Dashboard 的 Control Plan 保护。

如果在 Codex 沙盒里运行写入类命令，macOS 可能拦截 `crontab` 或
`~/.codex` 写入。AstrBot 控制台和普通终端没有这个沙盒限制。
