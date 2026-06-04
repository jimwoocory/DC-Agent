# DC-Agent 看门狗控制台

`watchdogctl` 是 DC-Agent 后台任务的统一控制入口，用来管理分散在
launchd、crontab、Codex automation 和 `dc-watchdog.sh` 探针里的任务。

## 常用命令

查看状态：

```bash
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh status nas
```

暂停或恢复一整个分组：

```bash
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh pause nas
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh resume nas
```

暂停或恢复单个任务：

```bash
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh pause-one launchd dianchi-tech-night
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh resume-one launchd dianchi-tech-night
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh pause-one cron dc-watchdog
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh resume-one cron dc-watchdog
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh pause-one codex nas
/Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.sh resume-one codex nas
```

支持的分组：

- `all`：全部已登记任务
- `nas`：NAS / 飞书同步、相关夜间日报、NAS workflow heartbeat
- `night`：夜间生成和上午推送类任务
- `sync`：文件同步类任务
- `watchdog`：看门狗和告警类任务
- `dianchi-tech`：巅池技术日报相关任务
- `onboarding`：入职问卷轮询任务

## 当前管理范围

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

## 注意

`pause` 不会删除脚本、plist、日志或 automation 文件，只会停用、卸载、移除
crontab 入口，或把 Codex automation 标记为 `PAUSED`。

如果在 Codex 沙盒里运行写入类命令，macOS 可能拦截 `crontab` 或
`~/.codex` 写入。AstrBot 控制台和普通终端没有这个沙盒限制。
