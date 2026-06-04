(function attachDcDashboardQuickEntries() {
  "use strict";

  var ROOT_ID = "dc-dashboard-quick-entries";
  var STATUS_URL = "/api/plug/system_entries/status";
  var WATCHDOG_URL = "/api/plug/system_entries/watchdog";
  var POLL_MS = 5000;
  var POS_KEY = "dc-dashboard-quick-entries-position-v2";
  var DEFAULT_ENTRIES = [
    {
      name: "Hermes Agent 官方 WebUI",
      url: "http://localhost:9119/",
      hint: "Hermes Agent 官方 UI / sessions 列表",
      alive: null,
    },
    {
      name: "Hermes Agent 第三方 WebUI",
      url: "http://localhost:8787/",
      hint: "EKKOLearnAI/hermes-web-ui 第三方界面",
      alive: null,
    },
    {
      name: "OpenClaw",
      url: "http://localhost:4312/",
      hint: "OpenClaw 控制台",
      alive: null,
      on_demand_kick: "http://localhost:9120/kick",
    },
  ];
  var GROUPS = ["nas", "watchdog", "night", "sync", "dianchi-tech", "onboarding"];
  var GROUP_LABELS = {
    nas: "NAS / 夜间任务",
    watchdog: "看门狗",
    night: "夜间任务",
    sync: "同步任务",
    "dianchi-tech": "巅池技术日报",
    onboarding: "入职轮询",
  };
  var STATUS_LABELS = {
    enabled: "已启用",
    disabled: "已停用",
    loaded: "运行中",
    "not-loaded": "未运行",
    installed: "已安装",
    "not-installed": "未安装",
    inaccessible: "无权限读取",
    ACTIVE: "运行中",
    PAUSED: "已暂停",
    missing: "文件缺失",
    unknown: "未知",
  };
  var TASK_LABELS = {
    "dianchi-tech-night": "技术日报夜间生成",
    "dianchi-tech-report": "技术日报上午推送",
    "baidu-nas-sync": "百度网盘同步",
    "feishu-sync": "飞书云盘同步",
    "nas-watchdog": "NAS 同步看门狗",
    "dc-watchdog": "DC-Agent 总看门狗",
    "dianchi-tech-cron": "旧版技术日报 cron",
    "onboarding-watch": "入职问卷轮询",
    nas: "NAS 夜间复盘",
    "nas-workflow": "飞书文档同步流程",
    "nas_watchdog_heartbeat": "NAS 看门狗心跳探针",
    "feishu_sync_heartbeat": "飞书同步心跳探针",
  };

  var state = {
    entries: DEFAULT_ENTRIES,
    statusAuthorized: true,
    lastError: "",
    panelOpen: false,
    watchdog: null,
    watchdogGroup: "nas",
    watchdogBusy: "",
    watchdogError: "",
  };

  function ensureStyles() {
    if (document.getElementById(ROOT_ID + "-styles")) {
      return;
    }

    var style = document.createElement("style");
    style.id = ROOT_ID + "-styles";
    style.textContent =
      "#" +
      ROOT_ID +
      "{" +
      "position:fixed;top:7px;right:clamp(220px,16vw,360px);z-index:2147483000;" +
      "width:max-content;max-width:min(96vw,1280px);height:38px;display:inline-flex;align-items:center;justify-content:center;gap:0;padding:0;overflow:visible;" +
      "border:1px solid rgba(148,163,184,.36);border-radius:8px;" +
      "background:rgba(255,255,255,.96);box-shadow:0 8px 24px rgba(15,23,42,.10);" +
      "backdrop-filter:blur(12px);font:13px/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#0f172a;" +
      "}" +
      "#" +
      ROOT_ID +
      "[hidden]{display:none!important}" +
      "#" +
      ROOT_ID +
      " .dcqe-drag{height:100%;width:34px;display:inline-flex;align-items:center;justify-content:center;border:0;background:transparent;color:#64748b;cursor:grab;border-right:1px solid rgba(148,163,184,.28);font-weight:800;line-height:1}" +
      "#" +
      ROOT_ID +
      " .dcqe-drag:active{cursor:grabbing}" +
      "#" +
      ROOT_ID +
      " .dcqe-title{height:100%;display:inline-flex;align-items:center;justify-content:center;color:#334155;font-weight:650;padding:0 18px;white-space:nowrap}" +
      "#" +
      ROOT_ID +
      " .dcqe-actions{display:flex;align-items:center;justify-content:center;gap:0;height:100%}" +
      "#" +
      ROOT_ID +
      " .dcqe-link,#" +
      ROOT_ID +
      " .dcqe-button{height:100%;display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:0 16px;" +
      "border:0;border-left:1px solid rgba(148,163,184,.28);border-radius:0;background:transparent;color:#0f172a;" +
      "text-decoration:none;font:inherit;font-weight:650;white-space:nowrap;transition:background .15s,color .15s;cursor:pointer}" +
      "#" +
      ROOT_ID +
      " .dcqe-link:hover,#" +
      ROOT_ID +
      " .dcqe-button:hover{background:rgba(59,130,246,.08);color:#0b3f8a}" +
      "#" +
      ROOT_ID +
      " .dcqe-dot{width:8px;height:8px;border-radius:50%;background:#94a3b8;box-shadow:none;flex:0 0 auto}" +
      "#" +
      ROOT_ID +
      " .dcqe-link[data-state='online'] .dcqe-dot{background:#10b981;box-shadow:none}" +
      "#" +
      ROOT_ID +
      " .dcqe-link[data-state='offline'] .dcqe-dot{background:#ef4444;box-shadow:none}" +
      "#" +
      ROOT_ID +
      " .dcqe-panel{position:absolute;top:48px;right:0;width:min(900px,calc(100vw - 20px));max-height:min(680px,calc(100vh - 76px));overflow:auto;border:1px solid rgba(148,163,184,.36);border-radius:8px;background:rgba(255,255,255,.98);box-shadow:0 18px 50px rgba(15,23,42,.18);padding:14px;color:#0f172a}" +
      "#" +
      ROOT_ID +
      " .dcqe-panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}" +
      "#" +
      ROOT_ID +
      " .dcqe-panel-title{font-weight:750;font-size:14px}" +
      "#" +
      ROOT_ID +
      " .dcqe-close{border:0;background:transparent;color:#64748b;cursor:pointer;font:inherit;font-size:18px;line-height:1;padding:2px 6px}" +
      "#" +
      ROOT_ID +
      " .dcqe-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:12px}" +
      "#" +
      ROOT_ID +
      " .dcqe-select,.dcqe-action{height:30px;border:1px solid rgba(148,163,184,.42);border-radius:7px;background:#fff;color:#0f172a;font:inherit;padding:0 10px}" +
      "#" +
      ROOT_ID +
      " .dcqe-action{cursor:pointer;font-weight:650}" +
      "#" +
      ROOT_ID +
      " .dcqe-action[data-kind='pause']{border-color:rgba(239,68,68,.42);color:#991b1b}" +
      "#" +
      ROOT_ID +
      " .dcqe-action[data-kind='resume']{border-color:rgba(16,185,129,.46);color:#047857}" +
      "#" +
      ROOT_ID +
      " .dcqe-action:disabled{opacity:.55;cursor:not-allowed}" +
      "#" +
      ROOT_ID +
      " .dcqe-msg{font-size:12px;color:#64748b;min-height:16px}" +
      "#" +
      ROOT_ID +
      " .dcqe-msg[data-error='true']{color:#b91c1c}" +
      "#" +
      ROOT_ID +
      " .dcqe-section{border-top:1px solid rgba(148,163,184,.22);padding-top:10px;margin-top:10px}" +
      "#" +
      ROOT_ID +
      " .dcqe-section:first-of-type{border-top:0;padding-top:0;margin-top:0}" +
      "#" +
      ROOT_ID +
      " .dcqe-section-title{font-size:12px;font-weight:750;color:#475569;margin-bottom:7px}" +
      "#" +
      ROOT_ID +
      " .dcqe-table{display:grid;grid-template-columns:minmax(150px,1fr) minmax(92px,.55fr) minmax(110px,.65fr) minmax(132px,.65fr);border:1px solid rgba(148,163,184,.24);border-radius:8px;overflow:hidden}" +
      "#" +
      ROOT_ID +
      " .dcqe-cell{padding:8px 10px;border-top:1px solid rgba(148,163,184,.18);font-size:12px;line-height:1.35;min-width:0;word-break:break-word;display:flex;align-items:center}" +
      "#" +
      ROOT_ID +
      " .dcqe-cell:nth-child(-n+4){border-top:0;background:rgba(241,245,249,.8);font-weight:700;color:#475569}" +
      "#" +
      ROOT_ID +
      " .dcqe-pill{display:inline-flex;align-items:center;min-height:20px;border-radius:999px;padding:2px 8px;background:#e2e8f0;color:#334155;font-weight:650}" +
      "#" +
      ROOT_ID +
      " .dcqe-pill[data-state*='disabled'],#" +
      ROOT_ID +
      " .dcqe-pill[data-state='PAUSED'],#" +
      ROOT_ID +
      " .dcqe-pill[data-state='not-installed'],#" +
      ROOT_ID +
      " .dcqe-pill[data-state='not-loaded']{background:#dcfce7;color:#166534}" +
      "#" +
      ROOT_ID +
      " .dcqe-pill[data-state*='enabled'],#" +
      ROOT_ID +
      " .dcqe-pill[data-state='ACTIVE'],#" +
      ROOT_ID +
      " .dcqe-pill[data-state='installed'],#" +
      ROOT_ID +
      " .dcqe-pill[data-state='loaded']{background:#fee2e2;color:#991b1b}" +
      "#" +
      ROOT_ID +
      " .dcqe-empty{font-size:12px;color:#64748b;padding:8px 0}" +
      "#" +
      ROOT_ID +
      " .dcqe-row-actions{display:flex;align-items:center;gap:6px;flex-wrap:wrap}" +
      "#" +
      ROOT_ID +
      " .dcqe-row-action{height:26px;border:1px solid rgba(148,163,184,.42);border-radius:7px;background:#fff;color:#0f172a;font:inherit;font-size:12px;font-weight:650;padding:0 9px;cursor:pointer}" +
      "#" +
      ROOT_ID +
      " .dcqe-row-action[data-kind='pause']{border-color:rgba(239,68,68,.42);color:#991b1b}" +
      "#" +
      ROOT_ID +
      " .dcqe-row-action[data-kind='resume']{border-color:rgba(16,185,129,.46);color:#047857}" +
      "#" +
      ROOT_ID +
      " .dcqe-row-action:disabled{opacity:.45;cursor:not-allowed}" +
      "@media (max-width: 760px){" +
      "#" +
      ROOT_ID +
      "{left:10px;right:10px;top:auto;bottom:12px;width:auto;height:42px;gap:0;padding:0;overflow:visible}" +
      "#" +
      ROOT_ID +
      " .dcqe-title{display:none}" +
      "#" +
      ROOT_ID +
      " .dcqe-actions{overflow-x:auto;flex:1}" +
      "#" +
      ROOT_ID +
      " .dcqe-link,#" +
      ROOT_ID +
      " .dcqe-button{flex:1;justify-content:center;min-width:max-content}" +
      "#" +
      ROOT_ID +
      " .dcqe-panel{top:auto;bottom:52px;left:0;right:0;width:auto}" +
      "}" +
      "@media (prefers-color-scheme: dark){" +
      "#" +
      ROOT_ID +
      "{background:rgba(15,23,42,.9);border-color:rgba(100,116,139,.46);box-shadow:0 8px 24px rgba(0,0,0,.28);color:#e5e7eb}" +
      "#" +
      ROOT_ID +
      " .dcqe-title,#" +
      ROOT_ID +
      " .dcqe-link,#" +
      ROOT_ID +
      " .dcqe-button{color:#e5e7eb}" +
      "#" +
      ROOT_ID +
      " .dcqe-link:hover,#" +
      ROOT_ID +
      " .dcqe-button:hover{background:rgba(59,130,246,.16);color:#bfdbfe}" +
      "#" +
      ROOT_ID +
      " .dcqe-panel{background:rgba(15,23,42,.97);border-color:rgba(100,116,139,.46);color:#e5e7eb}" +
      "#" +
      ROOT_ID +
      " .dcqe-select,#" +
      ROOT_ID +
      " .dcqe-action{background:#0f172a;color:#e5e7eb;border-color:rgba(100,116,139,.6)}" +
      "#" +
      ROOT_ID +
      " .dcqe-cell:nth-child(-n+4){background:rgba(30,41,59,.92);color:#cbd5e1}" +
      "#" +
      ROOT_ID +
      " .dcqe-row-action{background:#0f172a;color:#e5e7eb;border-color:rgba(100,116,139,.6)}" +
      "}";
    document.head.appendChild(style);
  }

  function stateName(alive) {
    if (alive === true) return "online";
    if (alive === false) return "offline";
    return "checking";
  }

  function stateLabel(alive) {
    if (alive === true) return "在线";
    if (alive === false) return "离线";
    return "检查中";
  }

  function visibleEntries() {
    return state.entries.filter(function (entry) {
      return entry && entry.url && /Hermes.*WebUI|OpenClaw/i.test(entry.name || "");
    });
  }

  function browserLocalUrl(url) {
    if (!url) return "";
    try {
      var parsed = new URL(url, window.location.href);
      if (
        (parsed.hostname === "127.0.0.1" || parsed.hostname === "::1") &&
        (window.location.hostname === "localhost" ||
          window.location.hostname === "127.0.0.1" ||
          window.location.hostname === "::1")
      ) {
        parsed.hostname = window.location.hostname;
      }
      return parsed.href;
    } catch (_) {
      return url;
    }
  }

  function statusText(value) {
    var key = String(value == null ? "" : value);
    return STATUS_LABELS[key] || key || "-";
  }

  function groupText(value) {
    return GROUP_LABELS[value] || value;
  }

  function taskText(value) {
    return TASK_LABELS[value] || value;
  }

  function groupsText(values) {
    return (values || []).map(groupText).join("、");
  }

  function requestWatchdog(action, group, targetType, targetKey) {
    var url =
      WATCHDOG_URL +
      "?action=" +
      encodeURIComponent(action || "status") +
      "&group=" +
      encodeURIComponent(group || state.watchdogGroup);
    if (targetType && targetKey) {
      url +=
        "&target_type=" +
        encodeURIComponent(targetType) +
        "&target_key=" +
        encodeURIComponent(targetKey);
    }
    return fetch(url, {
      credentials: "include",
      cache: "no-store",
      headers: { Accept: "application/json" },
    })
      .then(function (response) {
        if (response.status === 401 || response.status === 403) {
          throw new Error("请先登录 AstrBot");
        }
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (payload) {
        var data = payload && payload.data ? payload.data : payload;
        if (data && data.data) data = data.data;
        if (!data || !data.state) {
          throw new Error((payload && payload.message) || "watchdog state missing");
        }
        state.watchdog = data.state;
        state.watchdogError = "";
        return data.state;
      });
  }

  function ensureRoot() {
    ensureStyles();
    var root = document.getElementById(ROOT_ID);
    if (root) return root;

    root = document.createElement("nav");
    root.id = ROOT_ID;
    root.setAttribute("aria-label", "系统快捷入口");
    document.body.appendChild(root);
    installDrag(root);
    applySavedPosition(root);
    return root;
  }

  function installDrag(root) {
    var drag = { active: false, dx: 0, dy: 0 };
    root.addEventListener("pointerdown", function (event) {
      if (!event.target || !event.target.classList.contains("dcqe-drag")) return;
      drag.active = true;
      var rect = root.getBoundingClientRect();
      drag.dx = event.clientX - rect.left;
      drag.dy = event.clientY - rect.top;
      root.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    root.addEventListener("pointermove", function (event) {
      if (!drag.active) return;
      var nextLeft = Math.max(8, Math.min(window.innerWidth - root.offsetWidth - 8, event.clientX - drag.dx));
      var nextTop = Math.max(8, Math.min(window.innerHeight - root.offsetHeight - 8, event.clientY - drag.dy));
      root.style.left = nextLeft + "px";
      root.style.top = nextTop + "px";
      root.style.right = "auto";
      root.style.bottom = "auto";
      root.dataset.customPosition = "true";
    });
    root.addEventListener("pointerup", function () {
      if (!drag.active) return;
      drag.active = false;
      savePosition(root);
    });
  }

  function savePosition(root) {
    if (!root.dataset.customPosition) return;
    try {
      localStorage.setItem(POS_KEY, JSON.stringify({ left: root.style.left, top: root.style.top }));
    } catch (_) {}
  }

  function applySavedPosition(root) {
    try {
      var raw = localStorage.getItem(POS_KEY);
      if (!raw) return;
      var pos = JSON.parse(raw);
      if (!pos.left || !pos.top) return;
      root.style.left = pos.left;
      root.style.top = pos.top;
      root.style.right = "auto";
      root.style.bottom = "auto";
      root.dataset.customPosition = "true";
    } catch (_) {}
  }

  function openEntry(event, entry) {
    if (!entry || !entry.url) return;
    if (entry.alive !== false || !entry.on_demand_kick) return;
    event.preventDefault();
    var targetWindow = window.open("about:blank", "_blank");
    if (targetWindow) {
      targetWindow.opener = null;
    }
    fetch(browserLocalUrl(entry.on_demand_kick), { method: "GET", mode: "no-cors" }).catch(function () {
      return null;
    });
    window.setTimeout(function () {
      if (targetWindow) {
        targetWindow.location.href = browserLocalUrl(entry.url);
      } else {
        window.open(browserLocalUrl(entry.url), "_blank", "noopener");
      }
    }, 900);
  }

  function addCell(table, text, pillState) {
    var cell = document.createElement("div");
    cell.className = "dcqe-cell";
    if (text && typeof text.nodeType === "number") {
      cell.appendChild(text);
      table.appendChild(cell);
      return;
    }
    if (pillState) {
      var pill = document.createElement("span");
      pill.className = "dcqe-pill";
      pill.dataset.state = String(pillState);
      pill.textContent = statusText(text);
      cell.appendChild(pill);
    } else {
      cell.textContent = String(text == null ? "" : text);
    }
    table.appendChild(cell);
  }

  function renderTable(section, rows, columns) {
    if (!rows || rows.length === 0) {
      var empty = document.createElement("div");
      empty.className = "dcqe-empty";
      empty.textContent = "无项目";
      section.appendChild(empty);
      return;
    }
    var table = document.createElement("div");
    table.className = "dcqe-table";
    columns.forEach(function (c) {
      addCell(table, c.label);
    });
    rows.forEach(function (row) {
      columns.forEach(function (c) {
        var value = c.node ? c.node(row) : typeof c.value === "function" ? c.value(row) : row[c.value];
        addCell(table, value, c.pill ? value : "");
      });
    });
    section.appendChild(table);
  }

  function renderSection(panel, title, rows, columns) {
    var section = document.createElement("section");
    section.className = "dcqe-section";
    var h = document.createElement("div");
    h.className = "dcqe-section-title";
    h.textContent = title;
    section.appendChild(h);
    renderTable(section, rows, columns);
    panel.appendChild(section);
  }

  function rowActions(type, row, active) {
    var wrap = document.createElement("div");
    wrap.className = "dcqe-row-actions";
    [
      ["pause", "暂停", !!active],
      ["resume", "恢复", !active],
    ].forEach(function (item) {
      var btn = document.createElement("button");
      btn.className = "dcqe-row-action";
      btn.type = "button";
      btn.dataset.kind = item[0];
      btn.textContent = item[1];
      btn.disabled = !!state.watchdogBusy || !item[2];
      btn.addEventListener("click", function () {
        loadWatchdog(item[0], type, row.key);
      });
      wrap.appendChild(btn);
    });
    return wrap;
  }

  function readonlyAction(text) {
    var span = document.createElement("span");
    span.className = "dcqe-empty";
    span.textContent = text;
    return span;
  }

  function renderPanel(root) {
    if (!state.panelOpen) return;
    var panel = document.createElement("div");
    panel.className = "dcqe-panel";

    var head = document.createElement("div");
    head.className = "dcqe-panel-head";
    var title = document.createElement("div");
    title.className = "dcqe-panel-title";
    title.textContent = "Watch-Dog 控制台";
    var close = document.createElement("button");
    close.className = "dcqe-close";
    close.type = "button";
    close.textContent = "×";
    close.addEventListener("click", function () {
      state.panelOpen = false;
      render();
    });
    head.appendChild(title);
    head.appendChild(close);
    panel.appendChild(head);

    var toolbar = document.createElement("div");
    toolbar.className = "dcqe-toolbar";
    var select = document.createElement("select");
    select.className = "dcqe-select";
    GROUPS.forEach(function (g) {
      var option = document.createElement("option");
      option.value = g;
      option.textContent = groupText(g);
      option.selected = g === state.watchdogGroup;
      select.appendChild(option);
    });
    select.addEventListener("change", function () {
      state.watchdogGroup = select.value;
      loadWatchdog("status");
      render();
    });
    toolbar.appendChild(select);

    [
      ["status", "刷新"],
      ["pause", "暂停本组"],
      ["resume", "恢复本组"],
    ].forEach(function (pair) {
      var btn = document.createElement("button");
      btn.className = "dcqe-action";
      btn.type = "button";
      btn.dataset.kind = pair[0];
      btn.textContent = pair[1];
      btn.disabled = !!state.watchdogBusy;
      btn.addEventListener("click", function () {
        loadWatchdog(pair[0]);
      });
      toolbar.appendChild(btn);
    });
    panel.appendChild(toolbar);

    var msg = document.createElement("div");
    msg.className = "dcqe-msg";
    msg.dataset.error = state.watchdogError ? "true" : "false";
    msg.textContent = state.watchdogBusy || state.watchdogError || "";
    panel.appendChild(msg);

    if (state.watchdog) {
      renderSection(panel, "系统定时任务", state.watchdog.launchd, [
        { label: "任务", value: function (row) { return taskText(row.key); } },
        { label: "启用", value: "enabled_state", pill: true },
        { label: "加载", value: "loaded_state", pill: true },
        {
          label: "操作",
          node: function (row) {
            return rowActions("launchd", row, row.enabled_state === "enabled" || row.loaded_state === "loaded");
          },
        },
      ]);
      renderSection(panel, "CRON 任务", state.watchdog.cron, [
        { label: "任务", value: function (row) { return taskText(row.key); } },
        { label: "状态", value: "state", pill: true },
        { label: "说明", value: "description" },
        {
          label: "操作",
          node: function (row) {
            return rowActions("cron", row, row.state === "installed");
          },
        },
      ]);
      renderSection(panel, "Codex 自动化", state.watchdog.codex, [
        { label: "任务", value: function (row) { return taskText(row.key); } },
        { label: "状态", value: "status", pill: true },
        { label: "说明", value: "description" },
        {
          label: "操作",
          node: function (row) {
            return rowActions("codex", row, row.status === "ACTIVE");
          },
        },
      ]);
      renderSection(panel, "探针", state.watchdog.probes, [
        { label: "探针", value: function (row) { return taskText(row.key); } },
        { label: "状态", value: "state", pill: true },
        { label: "分组", value: function (row) { return groupsText(row.groups); } },
        { label: "操作", node: function () { return readonlyAction("由脚本管理"); } },
      ]);
    }

    root.appendChild(panel);
  }

  function render() {
    if (!document.body) return;

    var root = ensureRoot();
    var entries = visibleEntries();
    root.hidden = entries.length === 0;
    if (root.hidden) return;

    root.textContent = "";
    var drag = document.createElement("button");
    drag.className = "dcqe-drag";
    drag.type = "button";
    drag.title = "拖动";
    drag.textContent = "⋮⋮";
    root.appendChild(drag);

    var title = document.createElement("span");
    title.className = "dcqe-title";
    title.textContent = "系统入口";
    root.appendChild(title);

    var actions = document.createElement("div");
    actions.className = "dcqe-actions";
    root.appendChild(actions);

    entries.forEach(function (entry) {
      var link = document.createElement("a");
      var status = stateLabel(entry.alive);
      var href =
        entry.alive === false && entry.on_demand_kick
          ? browserLocalUrl(entry.url)
          : browserLocalUrl(entry.url);
      link.className = "dcqe-link";
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.dataset.state = stateName(entry.alive);
      link.title = (entry.hint || entry.name) + " - " + status;

      var dot = document.createElement("span");
      dot.className = "dcqe-dot";
      dot.setAttribute("aria-hidden", "true");
      link.appendChild(dot);

      var label = document.createElement("span");
      label.textContent = entry.name;
      link.appendChild(label);

      link.addEventListener("click", function (event) {
        openEntry(event, entry);
      });
      actions.appendChild(link);
    });

    var consoleButton = document.createElement("button");
    consoleButton.className = "dcqe-button";
    consoleButton.type = "button";
    consoleButton.textContent = "Watch-Dog 控制台";
    consoleButton.addEventListener("click", function () {
      state.panelOpen = !state.panelOpen;
      if (state.panelOpen && !state.watchdog) loadWatchdog("status");
      render();
    });
    actions.appendChild(consoleButton);
    renderPanel(root);
  }

  function normalizePayload(payload) {
    var data = payload && payload.data ? payload.data : payload;
    if (data && data.data && Array.isArray(data.data.entries)) data = data.data;
    if (!data || !Array.isArray(data.entries)) throw new Error("entries missing");
    return data.entries;
  }

  function refreshStatus() {
    fetch(STATUS_URL, {
      credentials: "include",
      cache: "no-store",
      headers: { Accept: "application/json" },
    })
      .then(function (response) {
        if (response.status === 401 || response.status === 403) {
          state.statusAuthorized = false;
          throw new Error("unauthorized");
        }
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (payload) {
        state.entries = normalizePayload(payload);
        state.statusAuthorized = true;
        state.lastError = "";
        render();
      })
      .catch(function (error) {
        state.lastError = error && error.message ? error.message : "status failed";
        if (state.lastError !== "unauthorized") state.statusAuthorized = true;
        render();
      });
  }

  function loadWatchdog(action, targetType, targetKey) {
    if (action === "status") {
      state.watchdogBusy = "刷新中";
    } else if (targetKey) {
      state.watchdogBusy = (action === "pause" ? "正在暂停：" : "正在恢复：") + targetKey;
    } else {
      state.watchdogBusy = action === "pause" ? "正在暂停本组" : "正在恢复本组";
    }
    state.watchdogError = "";
    render();
    requestWatchdog(action, state.watchdogGroup, targetType, targetKey)
      .catch(function (error) {
        state.watchdogError = error && error.message ? error.message : "看门狗控制失败";
      })
      .then(function () {
        state.watchdogBusy = "";
        render();
      });
  }

  function start() {
    render();
    refreshStatus();
    window.setInterval(refreshStatus, POLL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
