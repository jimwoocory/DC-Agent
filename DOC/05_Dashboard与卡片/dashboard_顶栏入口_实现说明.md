# Dashboard 顶栏快捷入口实现说明

## 目标

打开 AstrBot Dashboard 后，第一屏直接看到两个高频入口：

- Hermes WebUI：`http://localhost:9119/`
- OpenClaw 控制台：`http://localhost:4312/`

入口带在线/离线状态。OpenClaw 离线时点击会打开 `http://localhost:9120/kick`，由看门狗等待控制台启动后跳转。

## 当前做法

没有恢复旧的自定义 dashboard bundle，也没有修改登录逻辑。原因是旧 bundle 跟 AstrBot 版本耦合，5/11 到 5/14 升级已经证明容易被覆盖。

这次采用轻量注入：

1. `data/plugins/system_entries/main.py` 继续提供 `/api/plug/system_entries/status` 和 `/api/plug/system_entries/health`。
2. `data/plugins/system_entries/dc-dashboard-quick-entries.js` 是可回放的前端入口脚本。
3. `scripts-tools/install_dashboard_quick_entries.py` 把脚本复制到 `data/dist/assets/`，并在 `data/dist/index.html` 注入 script 标签。
4. `main.py` 启动时会自动执行一次回放。
5. Dashboard 在线升级接口下载新 WebUI 后，也会自动执行一次回放。
6. `scripts-watchdog/dc-watchdog.sh` 增加 `dashboard_quick_entries` 探活，发现入口脚本丢失或过期会报警。

## 手动回放

如果 AstrBot 升级后入口不见了，执行：

```bash
python3 scripts-tools/install_dashboard_quick_entries.py
```

只检查不修改：

```bash
python3 scripts-tools/install_dashboard_quick_entries.py --check
```

## 验收

- 打开 `http://localhost:6185/`，右上角能看到“系统入口 / Hermes WebUI / OpenClaw”。
- 两个入口可以一键打开。
- 状态点会随 `/api/plug/system_entries/status` 返回结果刷新。
- AstrBot 重启或 Dashboard 在线更新后，入口会自动补回。
