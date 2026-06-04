# Dashboard 顶栏快捷入口 — 下一步交接（给 codex）

**日期**：2026-05-15
**当前状态**：plugin page 方案做完但不是用户要的，需要换路线
**交接对象**：codex（用户在前面 debug 浪费了一上午，希望换执行人）

---

## 用户视角 · 一句话

我每天要点 **Hermes WebUI** 和 **OpenClaw 控制台**，以前 dashboard 右上角有两个按钮，5/11 升级 AstrBot v4.24.5 之后没了。这两个入口对我来说是高频，需要"打开 AstrBot 就能一眼看到、一键点开"。

---

## 当前状态（plugin page 方案 — 不是用户要的）

已做：
- `data/plugins/system_entries/` plugin
  - `main.py`：注册 `/api/plug/system_entries/{status,health}`，TCP 探活 4 个端口
  - `_conf_schema.json`：可配置 entries 列表 + cache TTL
  - `pages/dashboard/index.html`：4 张卡片 UI + 5s 轮询
- `scripts-watchdog/dc-watchdog.sh`：加了 `system_entries_plugin` HTTP 探活
- 修了 3 个 bug 才让 plugin page 跑通：
  1. JS 模板字符串语法错（else 分支单引号嵌套）
  2. 错用 `document.createElement('script')` dynamic load bridge-sdk —— **AstrBot 会自动重写静态 `<script src=...>` 加 asset_token**，dynamic 创建的反而绕过自动重写
  3. `bridge.apiGet(endpoint)` 的 endpoint 参数不能带前缀 —— dashboard SPA 会自动拼 `/api/plug/<pluginName>/`，传整段路径会双重前缀

**plugin page 现在工作了**，访问 dashboard → 左侧菜单 → "系统入口" 能看到 4 张服务卡片。

**但这不是用户要的** —— 用户要顶栏快捷按钮，plugin page 还要点进去看，没解决"高频访问要绕路"的痛点。

---

## 4 个候选方案（codex 评估）

### 方案 A：修复自定义 dashboard 适配 v4.24.5

- **怎么做**：恢复 `data/dist.custom-5.11-backup/` 为 `data/dist/`，但需要改前端登录代码：v4.24.5 后端期望明文密码，5/11 自定义 dashboard 把密码先 MD5 再 POST → 后端校验失败。
- **工作量**：找到自定义 dashboard 里 login 组件的 MD5 哈希逻辑（应该在 `assets/index-*.js` bundle 里），改成明文 POST。前端是 minified bundle，定位难度中。
- **风险**：每次 AstrBot 升级 dashboard 还是会被覆盖（同 5/11 → 5/14 教训），需要在 launchd / post-update hook 里加自动回放机制。
- **可逆性**：高，备份还在。
- **优点**：完全恢复原来的体验，按钮就在右上角。
- **缺点**：前端 patch 跟 AstrBot 升级耦合，每次升级要测。

### 方案 B：用 plugin 注入 dashboard 顶栏（AstrBot 官方扩展点？）

- **怎么做**：查 AstrBot v4.24.5 是否支持 plugin 注册"顶栏按钮"或"侧边栏快捷"。如果有，写个轻量 plugin 注册两个按钮 → 直接跳转 `:9119` / `:4312`。
- **工作量**：先调研 AstrBot 是否有这种扩展点。如果有，工作量极小（几十行）；如果没有，方案废。
- **风险**：低（如果有 API），高（如果没有，得 fork dashboard）。
- **可逆性**：高。
- **优点**：跟 AstrBot 升级解耦，跟 plugin page 同一个机制。
- **缺点**：不确定 v4.24.5 提供这种扩展点。

### 方案 C：保留 plugin page + 在 dashboard 主页加快捷卡片

- **怎么做**：在 AstrBot **dashboard 主页**（`/` 路径）加 "系统入口" 卡片直接展示 4 个服务的链接。但这需要改 `data/dist/`（默认 dashboard），又回到方案 A 的问题。
- **效果**：跟 plugin page 差不多，只是从"左侧菜单点进去"变成"主页直接显示"。
- **结论**：除非配合方案 A，否则等价于 plugin page。

### 方案 D：浏览器侧 — 油猴脚本 / 书签栏

- **怎么做**：写一个 Tampermonkey 脚本，访问 `localhost:6185/*` 时往顶栏注入两个按钮。
- **工作量**：30 行 JS。
- **风险**：跟 AstrBot 完全解耦，AstrBot 怎么升级都不影响。
- **可逆性**：100%（卸载脚本即可）。
- **优点**：极轻量，不动 AstrBot 任何代码。
- **缺点**：只对**装了脚本的浏览器**生效。如果用户多设备访问（手机 / iPad / 其他电脑），每个都要装。

---

## 推荐方案（请 codex 评估）

**建议 B → A → D 顺序尝试**：

1. **先调研方案 B**（30 分钟）：查 `astrbot/dashboard/routes/plugin.py` 和 `astrbot/core/star/context.py`，看 `register_web_api` 之外有没有 `register_dashboard_nav_button` 之类的扩展点。如果有 → 收工。
2. **方案 B 不可行就走方案 A**：定位自定义 dashboard 的 MD5 登录代码，改成明文 → 把 `data/dist.custom-5.11-backup/` 改完恢复为 `data/dist/`。**关键**：写一个 `scripts-tools/restore_custom_dashboard.sh`，在 AstrBot 每次升级后自动跑一次（或者 launchd post-start hook）。
3. **方案 A 太麻烦就 D 兜底**：油猴脚本附在 DOC 里，用户自己装。

---

## 给 codex 的具体输入

### 关键文件路径

| 文件 | 用途 |
|------|------|
| `data/plugins/system_entries/main.py` | 已注册的 plugin，**API 不要动**，方案 B 可能要在这里加 dashboard 注入逻辑 |
| `data/plugins/system_entries/pages/dashboard/index.html` | plugin page UI，可以保留作为"详情页" |
| `data/dist/` | AstrBot v4.24.5 默认 dashboard（当前激活的） |
| `data/dist.custom-5.11-backup/` | 5/11 时自定义带按钮的 dashboard（备份） |
| `astrbot/dashboard/routes/plugin.py` | dashboard 后端路由 + HTML 重写逻辑 |
| `astrbot/dashboard/plugin_page_bridge.js` | bridge SDK 源码（参考 apiGet 等扩展点） |
| `astrbot/core/star/context.py` | plugin Context API 定义（找扩展点） |
| `scripts-watchdog/dc-watchdog.sh` | watchdog 已加 system_entries_plugin 探活，不用动 |

### 关键调研问题

1. AstrBot v4.24.5 的 `Context` / `Star` 提供哪些扩展点？除了 `register_web_api`、`register_web_page` 之外还有什么？
2. dashboard 主页 (`data/dist/index.html` → bundle 入口) 是否有 "external nav items" / "plugin nav extensions" 配置？
3. 自定义 dashboard 的 MD5 登录逻辑在哪个 chunk？（grep `md5` / `crypto-js` 在 `data/dist.custom-5.11-backup/assets/`）

### 验收标准

用户视角的"完成"：
- [ ] 打开 `http://localhost:6185/` 第一屏能看到 Hermes WebUI / OpenClaw 入口（一键打开）
- [ ] AstrBot 下次升级（v4.24.6+）后入口**仍然在**（这是 5/11 教训的核心）
- [ ] 入口有"在线/离线"指示（参考现有 plugin page 的探活逻辑）

工程视角的"完成"：
- [ ] 实现选定方案
- [ ] 加 watchdog 探活（如果方案 A，新加 dashboard 自定义按钮的 mtime 探活）
- [ ] 写一个 `DOC/dashboard_顶栏入口_实现说明.md` 说清楚做法 + 升级回放步骤
- [ ] git commit + dc-agent/main 干净

---

## 历史包袱（避坑）

1. 5/11 → 5/14 升级 AstrBot 时 `data/dist/` 被覆盖，丢了所有自定义按钮 → 这次重做必须考虑**自动回放**。
2. 用户是产品经理（非工程师），交付物要用业务语言描述。
3. 用户偏好"业务低谷期重构"，不要"等稳定" —— 现在就是窗口期。
4. user 看到 "加载中..." 卡死会很挫败，UI 要 fail-fast + 有明确错误文案。

---

## 我（Claude）已经投入的时间

- plugin 注册 + UI 实现：~30 分钟
- debug 3 个 bug：~1.5 小时（应该 30 分钟内搞定，我没读 dashboard SPA 源码就猜，浪费了用户耐心 → 教训：先读 dashboard 的 `oe()` / `v()` 函数再下手）

希望 codex 接手后能少走弯路。
