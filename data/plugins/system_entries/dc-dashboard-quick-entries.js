(function attachDcDashboardQuickEntries() {
  "use strict";

  var ROOT_ID = "dc-dashboard-quick-entries";
  var STATUS_URL = "/api/plug/system_entries/status";
  var WATCHDOG_URL = "/api/plug/system_entries/watchdog";
  var POLL_MS = 5000;
  var POS_KEY = "dc-dashboard-quick-entries-position-v2";
  var DEFAULT_ENTRIES = [
    {
      name: "小助手健康",
      url: "/#/assistant-health",
      hint: "聊天线程、队列监听和会话存储健康面板",
      alive: null,
      pinned: true,
      category: "assistant",
      priority: 10,
    },
    {
      name: "小助手实时看板",
      url: "/#/live-monitor",
      hint: "小助手桌面会话、事件流和状态监控看板",
      alive: null,
      pinned: true,
      category: "assistant",
      priority: 20,
    },
    {
      name: "员工需求洞察",
      url: "/#/employee-insight",
      hint: "飞书私聊灰度验证、触达计划和员工需求闭环看板",
      alive: null,
      pinned: true,
      category: "assistant",
      priority: 60,
    },
    {
      name: "Hermes Agent 官方 WebUI",
      url: "http://localhost:9119/",
      hint: "Hermes Agent 官方 UI / sessions 列表",
      alive: null,
      category: "agent",
      priority: 30,
    },
    {
      name: "Hermes Agent 第三方 WebUI",
      url: "http://localhost:8787/",
      hint: "EKKOLearnAI/hermes-web-ui 第三方界面",
      alive: null,
      category: "agent",
      priority: 70,
    },
    {
      name: "记忆治理",
      url: "/#/memory-governance",
      hint: "NAS / Obsidian 记忆治理看板",
      alive: null,
      pinned: true,
      category: "governance",
      priority: 80,
    },
    {
      name: "内容 SOP 运营",
      url: "/#/content-sop-ops",
      hint: "内容 SOP 规则、记忆和审计运营看板",
      alive: null,
      pinned: true,
      category: "governance",
      priority: 90,
    },
  ];
  var CATEGORY_ORDER = ["assistant", "agent", "governance", "automation", "other"];
  var CATEGORY_LABELS = {
    all: "全部入口",
    assistant: "小助手",
    agent: "Agent 控制台",
    governance: "治理运营",
    automation: "自动化",
    other: "其他",
  };
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
    retired: "已退役",
    migration_required: "待迁移",
    review_required: "待人工介入",
    pending: "待接单",
    acknowledged: "处理中",
    resolved: "已解决",
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
    panelView: "entries",
    activeCategory: "all",
    searchText: "",
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
      "position:fixed;top:8px;left:clamp(104px,7vw,140px);right:auto;z-index:2147483000;" +
      "width:min(820px,calc(100vw - 260px));height:44px;display:flex;align-items:center;gap:6px;padding:4px;overflow:visible;" +
      "border:1px solid rgba(148,163,184,.34);border-radius:8px;background:rgba(248,250,252,.96);" +
      "box-shadow:0 12px 30px rgba(15,23,42,.12);backdrop-filter:blur(14px);font:13px/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#0f172a;" +
      "}" +
      "#" +
      ROOT_ID +
      "[hidden]{display:none!important}" +
      "#" +
      ROOT_ID +
      " .dcqe-drag{height:34px;width:26px;display:inline-flex;align-items:center;justify-content:center;border:0;border-radius:6px;background:transparent;color:#64748b;cursor:grab;font-weight:800;line-height:1;flex:0 0 auto}" +
      "#" +
      ROOT_ID +
      " .dcqe-drag:hover{background:rgba(148,163,184,.16)}" +
      "#" +
      ROOT_ID +
      " .dcqe-drag:active{cursor:grabbing}" +
      "#" +
      ROOT_ID +
      " .dcqe-title{height:34px;min-width:116px;display:flex;flex-direction:column;justify-content:center;gap:2px;border-right:1px solid rgba(148,163,184,.28);padding:0 10px 0 4px;color:#334155;white-space:nowrap;flex:0 0 auto}" +
      "#" +
      ROOT_ID +
      " .dcqe-title-main{font-size:13px;font-weight:760;letter-spacing:0}" +
      "#" +
      ROOT_ID +
      " .dcqe-title-sub{font-size:11px;color:#64748b;font-weight:560}" +
      "#" +
      ROOT_ID +
      " .dcqe-actions{display:flex;align-items:center;gap:6px;height:100%;min-width:0;flex:1}" +
      "#" +
      ROOT_ID +
      " .dcqe-rail{display:flex;align-items:center;gap:6px;height:100%;min-width:0;flex:1;overflow:hidden}" +
      "#" +
      ROOT_ID +
      " .dcqe-link,#" +
      ROOT_ID +
      " .dcqe-button{height:34px;display:inline-flex;align-items:center;justify-content:center;gap:7px;border:1px solid rgba(148,163,184,.26);border-radius:7px;background:#fff;color:#0f172a;text-decoration:none;font:inherit;font-weight:660;white-space:nowrap;transition:background .15s,border-color .15s,color .15s,transform .15s;cursor:pointer}" +
      "#" +
      ROOT_ID +
      " .dcqe-link{min-width:0;max-width:176px;padding:0 10px;flex:0 1 auto}" +
      "#" +
      ROOT_ID +
      " .dcqe-button{padding:0 11px;flex:0 0 auto}" +
      "#" +
      ROOT_ID +
      " .dcqe-link:hover,#" +
      ROOT_ID +
      " .dcqe-button:hover{background:#eff6ff;border-color:rgba(37,99,235,.28);color:#0b3f8a;transform:translateY(-1px)}" +
      "#" +
      ROOT_ID +
      " .dcqe-button[data-active='true']{background:#dbeafe;border-color:rgba(37,99,235,.38);color:#1d4ed8}" +
      "#" +
      ROOT_ID +
      " .dcqe-label{min-width:0;overflow:hidden;text-overflow:ellipsis}" +
      "#" +
      ROOT_ID +
      " .dcqe-dot{width:8px;height:8px;border-radius:50%;background:#94a3b8;box-shadow:none;flex:0 0 auto}" +
      "#" +
      ROOT_ID +
      " .dcqe-link[data-state='online'] .dcqe-dot,#" +
      ROOT_ID +
      " .dcqe-entry-card[data-state='online'] .dcqe-dot{background:#10b981}" +
      "#" +
      ROOT_ID +
      " .dcqe-link[data-state='offline'] .dcqe-dot,#" +
      ROOT_ID +
      " .dcqe-entry-card[data-state='offline'] .dcqe-dot{background:#ef4444}" +
      "#" +
      ROOT_ID +
      " .dcqe-link[data-state='warning'] .dcqe-dot,#" +
      ROOT_ID +
      " .dcqe-entry-card[data-state='warning'] .dcqe-dot{background:#f59e0b}" +
      "#" +
      ROOT_ID +
      " .dcqe-panel{position:absolute;top:54px;right:0;width:min(780px,calc(100vw - 28px));max-height:min(720px,calc(100vh - 86px));overflow:auto;border:1px solid rgba(148,163,184,.34);border-radius:8px;background:rgba(255,255,255,.98);box-shadow:0 24px 64px rgba(15,23,42,.20);color:#0f172a}" +
      "#" +
      ROOT_ID +
      " .dcqe-panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;padding:16px 16px 12px;border-bottom:1px solid rgba(148,163,184,.20)}" +
      "#" +
      ROOT_ID +
      " .dcqe-panel-kicker{font-size:11px;font-weight:760;color:#2563eb;margin-bottom:4px}" +
      "#" +
      ROOT_ID +
      " .dcqe-panel-title{font-weight:780;font-size:16px;letter-spacing:0}" +
      "#" +
      ROOT_ID +
      " .dcqe-panel-subtitle{font-size:12px;color:#64748b;margin-top:5px;line-height:1.45}" +
      "#" +
      ROOT_ID +
      " .dcqe-close{height:30px;width:30px;border:1px solid rgba(148,163,184,.32);border-radius:7px;background:#fff;color:#64748b;cursor:pointer;font:inherit;font-size:18px;line-height:1;flex:0 0 auto}" +
      "#" +
      ROOT_ID +
      " .dcqe-mode-switch{display:flex;align-items:center;gap:6px;padding:12px 16px 0}" +
      "#" +
      ROOT_ID +
      " .dcqe-mode{height:30px;border:1px solid rgba(148,163,184,.30);border-radius:7px;background:#fff;color:#334155;font:inherit;font-weight:700;padding:0 10px;cursor:pointer}" +
      "#" +
      ROOT_ID +
      " .dcqe-mode[data-active='true']{background:#0f172a;border-color:#0f172a;color:#fff}" +
      "#" +
      ROOT_ID +
      " .dcqe-panel-controls{display:flex;align-items:center;gap:8px;padding:12px 16px;flex-wrap:wrap}" +
      "#" +
      ROOT_ID +
      " .dcqe-search{height:32px;min-width:180px;flex:1;border:1px solid rgba(148,163,184,.36);border-radius:7px;background:#fff;color:#0f172a;font:inherit;padding:0 10px}" +
      "#" +
      ROOT_ID +
      " .dcqe-tabs{display:flex;align-items:center;gap:6px;flex-wrap:wrap}" +
      "#" +
      ROOT_ID +
      " .dcqe-tab{height:30px;border:1px solid rgba(148,163,184,.30);border-radius:999px;background:#fff;color:#334155;font:inherit;font-weight:650;padding:0 10px;cursor:pointer}" +
      "#" +
      ROOT_ID +
      " .dcqe-tab[data-active='true']{background:#eef2ff;border-color:rgba(79,70,229,.32);color:#3730a3}" +
      "#" +
      ROOT_ID +
      " .dcqe-entry-directory{padding:0 16px 16px}" +
      "#" +
      ROOT_ID +
      " .dcqe-entry-summary{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:12px}" +
      "#" +
      ROOT_ID +
      " .dcqe-count{display:inline-flex;align-items:center;gap:6px;min-height:24px;border-radius:999px;background:#f1f5f9;color:#334155;font-size:12px;font-weight:700;padding:3px 9px}" +
      "#" +
      ROOT_ID +
      " .dcqe-group{margin-top:14px}" +
      "#" +
      ROOT_ID +
      " .dcqe-group:first-child{margin-top:0}" +
      "#" +
      ROOT_ID +
      " .dcqe-group-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}" +
      "#" +
      ROOT_ID +
      " .dcqe-section-title{font-size:12px;font-weight:780;color:#475569}" +
      "#" +
      ROOT_ID +
      " .dcqe-section-count{font-size:11px;color:#64748b;font-weight:700}" +
      "#" +
      ROOT_ID +
      " .dcqe-panel-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}" +
      "#" +
      ROOT_ID +
      " .dcqe-entry-card{min-height:104px;display:flex;flex-direction:column;justify-content:space-between;gap:10px;border:1px solid rgba(148,163,184,.28);border-radius:8px;background:linear-gradient(180deg,#fff,#f8fafc);color:#0f172a;text-decoration:none;padding:12px;transition:border-color .15s,box-shadow .15s,transform .15s}" +
      "#" +
      ROOT_ID +
      " .dcqe-entry-card:hover{border-color:rgba(37,99,235,.34);box-shadow:0 12px 28px rgba(15,23,42,.10);transform:translateY(-1px)}" +
      "#" +
      ROOT_ID +
      " .dcqe-card-top{display:flex;align-items:flex-start;gap:9px;min-width:0}" +
      "#" +
      ROOT_ID +
      " .dcqe-card-copy{min-width:0;flex:1}" +
      "#" +
      ROOT_ID +
      " .dcqe-card-title{font-size:13px;font-weight:760;color:#0f172a;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" +
      "#" +
      ROOT_ID +
      " .dcqe-card-hint{font-size:12px;color:#64748b;line-height:1.45;margin-top:5px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}" +
      "#" +
      ROOT_ID +
      " .dcqe-card-meta{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:12px;color:#64748b}" +
      "#" +
      ROOT_ID +
      " .dcqe-open-label{font-weight:760;color:#2563eb}" +
      "#" +
      ROOT_ID +
      " .dcqe-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:12px 16px}" +
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
      " .dcqe-msg{font-size:12px;color:#64748b;min-height:16px;padding:0 16px 4px}" +
      "#" +
      ROOT_ID +
      " .dcqe-msg[data-error='true']{color:#b91c1c}" +
      "#" +
      ROOT_ID +
      " .dcqe-section{border-top:1px solid rgba(148,163,184,.22);padding:12px 16px 0;margin-top:10px}" +
      "#" +
      ROOT_ID +
      " .dcqe-section:first-of-type{border-top:0;margin-top:0}" +
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
      " .dcqe-pill[data-state*='enabled'],#" +
      ROOT_ID +
      " .dcqe-pill[data-state='ACTIVE'],#" +
      ROOT_ID +
      " .dcqe-pill[data-state='installed'],#" +
      ROOT_ID +
      " .dcqe-pill[data-state='loaded']{background:#dcfce7;color:#166534}" +
      "#" +
      ROOT_ID +
      " .dcqe-pill[data-state*='disabled'],#" +
      ROOT_ID +
      " .dcqe-pill[data-state='PAUSED'],#" +
      ROOT_ID +
      " .dcqe-pill[data-state='not-installed'],#" +
      ROOT_ID +
      " .dcqe-pill[data-state='not-loaded']{background:#fef3c7;color:#92400e}" +
      "#" +
      ROOT_ID +
      " .dcqe-empty{font-size:12px;color:#64748b;padding:10px 0}" +
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
      "@media (max-width: 980px){" +
      "#" +
      ROOT_ID +
      "{left:12px;right:12px;width:auto}" +
      "}" +
      "@media (max-width: 760px){" +
      "#" +
      ROOT_ID +
      "{top:auto;bottom:12px;height:46px}" +
      "#" +
      ROOT_ID +
      " .dcqe-title{min-width:84px;border-right:0;padding-right:4px}" +
      "#" +
      ROOT_ID +
      " .dcqe-title-sub{display:none}" +
      "#" +
      ROOT_ID +
      " .dcqe-rail .dcqe-link:nth-of-type(n+2){display:none}" +
      "#" +
      ROOT_ID +
      " .dcqe-button{padding:0 9px}" +
      "#" +
      ROOT_ID +
      " .dcqe-panel{top:auto;bottom:56px;left:0;right:0;width:auto;max-height:calc(100vh - 84px)}" +
      "#" +
      ROOT_ID +
      " .dcqe-panel-grid{grid-template-columns:1fr}" +
      "}" +
      "@media (prefers-color-scheme: dark){" +
      "#" +
      ROOT_ID +
      "{background:rgba(15,23,42,.92);border-color:rgba(100,116,139,.46);box-shadow:0 12px 30px rgba(0,0,0,.32);color:#e5e7eb}" +
      "#" +
      ROOT_ID +
      " .dcqe-title,#" +
      ROOT_ID +
      " .dcqe-link,#" +
      ROOT_ID +
      " .dcqe-button{color:#e5e7eb}" +
      "#" +
      ROOT_ID +
      " .dcqe-link,#" +
      ROOT_ID +
      " .dcqe-button,#" +
      ROOT_ID +
      " .dcqe-close,#" +
      ROOT_ID +
      " .dcqe-mode,#" +
      ROOT_ID +
      " .dcqe-tab,#" +
      ROOT_ID +
      " .dcqe-search{background:#0f172a;border-color:rgba(100,116,139,.58);color:#e5e7eb}" +
      "#" +
      ROOT_ID +
      " .dcqe-link:hover,#" +
      ROOT_ID +
      " .dcqe-button:hover{background:rgba(59,130,246,.16);color:#bfdbfe}" +
      "#" +
      ROOT_ID +
      " .dcqe-panel{background:rgba(15,23,42,.98);border-color:rgba(100,116,139,.46);color:#e5e7eb}" +
      "#" +
      ROOT_ID +
      " .dcqe-entry-card{background:linear-gradient(180deg,#111827,#0f172a);border-color:rgba(100,116,139,.48);color:#e5e7eb}" +
      "#" +
      ROOT_ID +
      " .dcqe-card-title{color:#e5e7eb}" +
      "#" +
      ROOT_ID +
      " .dcqe-panel-subtitle,#" +
      ROOT_ID +
      " .dcqe-card-hint,#" +
      ROOT_ID +
      " .dcqe-card-meta,#" +
      ROOT_ID +
      " .dcqe-title-sub,#" +
      ROOT_ID +
      " .dcqe-section-count{color:#94a3b8}" +
      "#" +
      ROOT_ID +
      " .dcqe-count{background:rgba(30,41,59,.92);color:#cbd5e1}" +
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

  function stateName(alive, availability) {
    if (availability === "ready") return "online";
    if (availability === "offline") return "offline";
    if (availability === "wrong_service" || availability === "tcp_listening")
      return "warning";
    if (availability) return "checking";
    if (alive === true) return "checking";
    if (alive === false) return "offline";
    return "checking";
  }

  function stateLabel(alive, availability) {
    if (availability === "ready") return "在线";
    if (availability === "offline") return "离线";
    if (availability === "wrong_service") return "服务不匹配";
    if (availability === "tcp_listening") return "仅端口监听";
    if (availability) return "检查中";
    if (alive === true) return "检查中";
    if (alive === false) return "离线";
    return "检查中";
  }

  function categoryText(value) {
    return CATEGORY_LABELS[value] || value || CATEGORY_LABELS.other;
  }

  function entryCategory(entry) {
    var category = entry && entry.category ? String(entry.category) : "";
    if (CATEGORY_LABELS[category]) return category;
    var name = String((entry && entry.name) || "");
    if (/小助手|员工需求/.test(name)) return "assistant";
    if (/Hermes|Agent/i.test(name)) return "agent";
    if (/记忆|SOP|内容|治理/.test(name)) return "governance";
    return "other";
  }

  function categoryRank(category) {
    var index = CATEGORY_ORDER.indexOf(category);
    return index >= 0 ? index : CATEGORY_ORDER.length;
  }

  function entryPriority(entry) {
    var priority = Number(entry && entry.priority);
    if (Number.isFinite(priority)) return priority;
    if (entry && entry.pinned === true) return 50;
    return 100;
  }

  function enrichEntry(entry) {
    var match = DEFAULT_ENTRIES.find(function (defaultEntry) {
      return (
        defaultEntry &&
        entry &&
        (defaultEntry.name === entry.name || defaultEntry.url === entry.url)
      );
    });
    var enriched = Object.assign({}, match || {}, entry || {});
    enriched.category = entryCategory(enriched);
    enriched.priority = entryPriority(enriched);
    return enriched;
  }

  function sortEntries(entries) {
    return entries.slice().sort(function (a, b) {
      var byPriority = entryPriority(a) - entryPriority(b);
      if (byPriority !== 0) return byPriority;
      var byCategory = categoryRank(entryCategory(a)) - categoryRank(entryCategory(b));
      if (byCategory !== 0) return byCategory;
      return String(a.name || "").localeCompare(String(b.name || ""), "zh-Hans-CN");
    });
  }

  function visibleEntries() {
    return sortEntries(
      state.entries.filter(function (entry) {
        return (
          entry &&
          entry.url &&
          (entry.pinned === true || /Hermes.*WebUI/i.test(entry.name || ""))
        );
      })
    );
  }

  function primaryEntries(entries) {
    return entries.slice(0, 3);
  }

  function entryMatchesSearch(entry) {
    var query = state.searchText.trim().toLowerCase();
    if (!query) return true;
    return [entry.name, entry.hint, entry.url, categoryText(entryCategory(entry))]
      .join(" ")
      .toLowerCase()
      .indexOf(query) >= 0;
  }

  function filteredEntries(entries) {
    return entries.filter(function (entry) {
      if (state.activeCategory !== "all" && entryCategory(entry) !== state.activeCategory) {
        return false;
      }
      return entryMatchesSearch(entry);
    });
  }

  function entriesByCategory(entries) {
    var groups = {};
    entries.forEach(function (entry) {
      var category = entryCategory(entry);
      if (!groups[category]) groups[category] = [];
      groups[category].push(entry);
    });
    return groups;
  }

  function summarizeEntries(entries) {
    var summary = { total: entries.length, online: 0, warning: 0, offline: 0, checking: 0 };
    entries.forEach(function (entry) {
      var stateValue = stateName(entry.alive, entry.availability);
      if (stateValue === "online") summary.online += 1;
      else if (stateValue === "warning") summary.warning += 1;
      else if (stateValue === "offline") summary.offline += 1;
      else summary.checking += 1;
    });
    return summary;
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

  function requestWatchdog(action, group, targetType, targetKey, planId, reviewAction) {
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
    if (planId) {
      url += "&plan_id=" + encodeURIComponent(planId);
    }
    if (reviewAction) {
      url += "&review_action=" + encodeURIComponent(reviewAction);
    }
    return fetch(url, {
      method: action === "pause" || action === "resume" || action === "review" ? "POST" : "GET",
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
        if (!data || (!data.state && !data.plan)) {
          throw new Error((payload && payload.message) || "watchdog state missing");
        }
        if (data.state) state.watchdog = data.state;
        state.watchdogError = "";
        return data;
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
        loadWatchdog(item[0], type, row.key, row);
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

  function repairReviewAction(row) {
    var operation = row.status === "pending" ? "acknowledge" : row.status === "acknowledged" ? "resolve" : "";
    if (!operation) return readonlyAction(row.status === "resolved" ? "已解决" : "状态不可操作");
    var button = document.createElement("button");
    button.className = "dcqe-row-action";
    button.type = "button";
    button.textContent = operation === "acknowledge" ? "接单" : "确认解决";
    button.disabled = !!state.watchdogBusy;
    button.addEventListener("click", function () {
      state.watchdogBusy = "正在生成审核 Control Plan";
      state.watchdogError = "";
      render();
      requestWatchdog("plan-review", "watchdog", "repair_review", row.incident_id, "", operation)
        .then(function (data) {
          var plan = data && data.plan;
          if (!plan || plan.incident_id !== row.incident_id || plan.operation !== operation) {
            throw new Error("审核 Control Plan 缺失或不匹配");
          }
          var confirmed = window.confirm(
            (operation === "acknowledge" ? "接单处理" : "确认已解决") +
            " Incident " + row.incident_id + "？\n\n" +
            (row.summary || row.reason || "需要人工复核") +
            "\n\n此操作只更新审核状态，不执行修复命令。计划 120 秒内有效。"
          );
          if (!confirmed) return null;
          state.watchdogBusy = "正在应用审核 Control Plan";
          render();
          return requestWatchdog(
            "review",
            "watchdog",
            "repair_review",
            row.incident_id,
            plan.plan_id,
            operation
          );
        })
        .catch(function (error) {
          state.watchdogError = error && error.message ? error.message : "审核 Control Plan 失败";
        })
        .then(function () {
          state.watchdogBusy = "";
          render();
        });
    });
    return button;
  }

  function createEntryLink(entry) {
    var link = document.createElement("a");
    var status = stateLabel(entry.alive, entry.availability);
    link.className = "dcqe-link";
    link.href = browserLocalUrl(entry.url);
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.dataset.state = stateName(entry.alive, entry.availability);
    link.dataset.category = entryCategory(entry);
    link.title = (entry.hint || entry.name) + " - " + status;

    var dot = document.createElement("span");
    dot.className = "dcqe-dot";
    dot.setAttribute("aria-hidden", "true");
    link.appendChild(dot);

    var label = document.createElement("span");
    label.className = "dcqe-label";
    label.textContent = entry.name;
    link.appendChild(label);

    link.addEventListener("click", function (event) {
      openEntry(event, entry);
    });
    return link;
  }

  function appendCount(parent, label, value, stateValue) {
    var item = document.createElement("span");
    item.className = "dcqe-count";
    if (stateValue) item.dataset.state = stateValue;
    item.textContent = label + " " + value;
    parent.appendChild(item);
  }

  function renderModeSwitch(panel) {
    var wrap = document.createElement("div");
    wrap.className = "dcqe-mode-switch";
    [
      ["entries", "入口总览"],
      ["watchdog", "Watch-Dog"],
    ].forEach(function (item) {
      var btn = document.createElement("button");
      btn.className = "dcqe-mode";
      btn.type = "button";
      btn.dataset.active = state.panelView === item[0] ? "true" : "false";
      btn.textContent = item[1];
      btn.addEventListener("click", function () {
        state.panelView = item[0];
        state.panelOpen = true;
        if (state.panelView === "watchdog" && !state.watchdog) loadWatchdog("status");
        render();
      });
      wrap.appendChild(btn);
    });
    panel.appendChild(wrap);
  }

  function renderEntryCard(grid, entry) {
    var card = document.createElement("a");
    var status = stateLabel(entry.alive, entry.availability);
    card.className = "dcqe-entry-card";
    card.href = browserLocalUrl(entry.url);
    card.target = "_blank";
    card.rel = "noopener noreferrer";
    card.dataset.state = stateName(entry.alive, entry.availability);
    card.dataset.category = entryCategory(entry);
    card.title = (entry.hint || entry.name) + " - " + status;

    var top = document.createElement("div");
    top.className = "dcqe-card-top";
    var dot = document.createElement("span");
    dot.className = "dcqe-dot";
    dot.setAttribute("aria-hidden", "true");
    top.appendChild(dot);

    var copy = document.createElement("div");
    copy.className = "dcqe-card-copy";
    var title = document.createElement("div");
    title.className = "dcqe-card-title";
    title.textContent = entry.name;
    copy.appendChild(title);
    var hint = document.createElement("div");
    hint.className = "dcqe-card-hint";
    hint.textContent = entry.hint || categoryText(entryCategory(entry));
    copy.appendChild(hint);
    top.appendChild(copy);
    card.appendChild(top);

    var meta = document.createElement("div");
    meta.className = "dcqe-card-meta";
    var category = document.createElement("span");
    category.textContent = categoryText(entryCategory(entry)) + " · " + status;
    meta.appendChild(category);
    var open = document.createElement("span");
    open.className = "dcqe-open-label";
    open.textContent = "打开";
    meta.appendChild(open);
    card.appendChild(meta);

    card.addEventListener("click", function (event) {
      openEntry(event, entry);
    });
    grid.appendChild(card);
  }

  function renderCategoryTabs(parent, entries) {
    var tabs = document.createElement("div");
    tabs.className = "dcqe-tabs";
    var counts = entriesByCategory(entries);
    var categoryKeys = ["all"].concat(
      CATEGORY_ORDER.filter(function (category) {
        return category !== "other" ? counts[category] && counts[category].length : counts[category];
      })
    );
    if (counts.other && counts.other.length && categoryKeys.indexOf("other") < 0) {
      categoryKeys.push("other");
    }
    categoryKeys.forEach(function (category) {
      var count = category === "all" ? entries.length : (counts[category] || []).length;
      var tab = document.createElement("button");
      tab.className = "dcqe-tab";
      tab.type = "button";
      tab.dataset.active = state.activeCategory === category ? "true" : "false";
      tab.textContent = categoryText(category) + " " + count;
      tab.addEventListener("click", function () {
        state.activeCategory = category;
        render();
      });
      tabs.appendChild(tab);
    });
    parent.appendChild(tabs);
  }

  function renderEntryDirectory(panel, entries) {
    var controls = document.createElement("div");
    controls.className = "dcqe-panel-controls";
    var search = document.createElement("input");
    search.className = "dcqe-search";
    search.type = "search";
    search.placeholder = "搜索入口、分类或说明";
    search.value = state.searchText;
    search.addEventListener("input", function () {
      state.searchText = search.value;
      window.requestAnimationFrame(function () {
        render();
        var next = document.querySelector("#" + ROOT_ID + " .dcqe-search");
        if (next) {
          next.focus();
          try {
            next.setSelectionRange(next.value.length, next.value.length);
          } catch (_) {}
        }
      });
    });
    controls.appendChild(search);
    renderCategoryTabs(controls, entries);
    panel.appendChild(controls);

    var body = document.createElement("div");
    body.className = "dcqe-entry-directory";
    var summary = summarizeEntries(entries);
    var summaryNode = document.createElement("div");
    summaryNode.className = "dcqe-entry-summary";
    appendCount(summaryNode, "全部", summary.total);
    appendCount(summaryNode, "在线", summary.online, "online");
    appendCount(summaryNode, "关注", summary.warning + summary.offline, "warning");
    appendCount(summaryNode, "检查中", summary.checking, "checking");
    body.appendChild(summaryNode);

    var scopedEntries = filteredEntries(entries);
    if (scopedEntries.length === 0) {
      var empty = document.createElement("div");
      empty.className = "dcqe-empty";
      empty.textContent = "没有匹配的入口";
      body.appendChild(empty);
      panel.appendChild(body);
      return;
    }

    var groups = entriesByCategory(scopedEntries);
    CATEGORY_ORDER.forEach(function (category) {
      var rows = groups[category];
      if (!rows || rows.length === 0) return;
      var section = document.createElement("section");
      section.className = "dcqe-group";
      var head = document.createElement("div");
      head.className = "dcqe-group-head";
      var title = document.createElement("div");
      title.className = "dcqe-section-title";
      title.textContent = categoryText(category);
      var count = document.createElement("div");
      count.className = "dcqe-section-count";
      count.textContent = rows.length + " 个入口";
      head.appendChild(title);
      head.appendChild(count);
      section.appendChild(head);
      var grid = document.createElement("div");
      grid.className = "dcqe-panel-grid";
      rows.forEach(function (entry) {
        renderEntryCard(grid, entry);
      });
      section.appendChild(grid);
      body.appendChild(section);
    });
    panel.appendChild(body);
  }

  function renderWatchdogPanel(panel) {
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
      var controlPlane = state.watchdog.control_plane;
      if (controlPlane && Array.isArray(controlPlane.tasks)) {
        renderSection(panel, "统一任务视图", controlPlane.tasks, [
          { label: "任务", value: "name" },
          { label: "状态", value: "status", pill: true },
          { label: "来源", value: "source" },
          { label: "执行角色", value: "executor_role" },
          { label: "控制", value: "control_mode" },
          { label: "权威", value: "authority_state" },
          { label: "操作", node: function (row) { return readonlyAction(row.authority_state === "superseded" ? "已退役" : "统一只读视图"); } },
        ]);
      }
      var repair = state.watchdog.repair;
      if (repair && Array.isArray(repair.services)) {
        var repairAnalytics = repair.analytics && repair.analytics.dashboard_summary;
        if (repairAnalytics) {
          renderSection(panel, "自愈可靠性指标", [repairAnalytics], [
            { label: "状态", value: "status", pill: true },
            { label: "24h 自动尝试", value: "auto_attempts_24h" },
            { label: "24h 成功率", value: "success_rate_24h" },
            { label: "24h 平均 MTTR(秒)", value: "mttr_mean_seconds_24h" },
            { label: "当前熔断", value: "open_circuits" },
            { label: "开放审核", value: "open_reviews" },
            { label: "审核超 60m", value: "pending_over_60m" },
            { label: "保留预览", value: "retention_candidates" },
            { label: "Codex 深巡检建议", value: "codex_review_recommended", pill: true },
            { label: "操作", node: function () { return readonlyAction("指标只读"); } },
          ]);
        }
        renderSection(panel, "Watchdog 自愈策略", repair.services, [
          { label: "服务", value: "service" },
          { label: "状态", value: "state", pill: true },
          { label: "剩余尝试", value: "attempts_remaining" },
          { label: "连续失败", value: "consecutive_failures" },
          { label: "熔断剩余秒数", value: "circuit_remaining_seconds" },
          { label: "最近 Incident", value: "last_incident_id" },
          { label: "操作", node: function () { return readonlyAction("策略只读"); } },
        ]);
        renderSection(panel, "人工介入队列", repair.reviews || [], [
          { label: "Incident", value: "incident_id" },
          { label: "服务", value: "service" },
          { label: "状态", value: "status", pill: true },
          { label: "风险", value: "risk" },
          { label: "已通知", value: function (row) { return (row.notification_stages || []).join("、"); } },
          { label: "摘要", value: "summary" },
          { label: "原因", value: "reason" },
          { label: "操作", node: repairReviewAction },
        ]);
        renderSection(panel, "最近自愈结果", repair.recent_results || [], [
          { label: "Incident", value: "incident_id" },
          { label: "服务", value: "service" },
          { label: "动作", value: "action_id" },
          { label: "状态", value: "status", pill: true },
          { label: "原因", value: "reason" },
          { label: "验证", value: "verification_detail" },
          { label: "操作", node: function () { return readonlyAction("审计只读"); } },
        ]);
      }
      renderSection(panel, "系统定时任务", state.watchdog.launchd, [
        { label: "任务", value: function (row) { return taskText(row.key); } },
        { label: "启用", value: "enabled_state", pill: true },
        { label: "加载", value: "loaded_state", pill: true },
        {
          label: "操作",
          node: function (row) {
            if (row.controllable === false) return readonlyAction("只读登记");
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
            if (row.controllable === false) return readonlyAction("只读登记");
            return rowActions("cron", row, row.state === "installed");
          },
        },
      ]);
      renderSection(panel, "Codex 重要工具", state.watchdog.codex_tools || [], [
        { label: "能力", value: "name" },
        { label: "用途", value: "description" },
        {
          label: "授权",
          value: function (row) {
            return (row.authorized_by || []).join("、");
          },
        },
        {
          label: "入口",
          value: function (row) {
            return (row.entrypoints || []).join("；");
          },
        },
        { label: "调度权", node: function () { return readonlyAction("不拥有"); } },
      ]);
      renderSection(panel, "旧 Codex 自动任务（已停用）", state.watchdog.codex, [
        { label: "任务", value: function (row) { return taskText(row.key); } },
        { label: "状态", value: "status", pill: true },
        { label: "说明", value: "description" },
        {
          label: "操作",
          node: function (row) {
            if (row.status !== "ACTIVE") return readonlyAction("不可恢复");
            return rowActions("codex", row, true);
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

  }

  function renderPanel(root, entries) {
    if (!state.panelOpen) return;
    var panel = document.createElement("div");
    panel.className = "dcqe-panel";

    var head = document.createElement("div");
    head.className = "dcqe-panel-head";
    var copy = document.createElement("div");
    var kicker = document.createElement("div");
    kicker.className = "dcqe-panel-kicker";
    kicker.textContent = "DC-Agent";
    var title = document.createElement("div");
    title.className = "dcqe-panel-title";
    title.textContent = state.panelView === "watchdog" ? "任务控制面" : "系统入口中枢";
    var subtitle = document.createElement("div");
    subtitle.className = "dcqe-panel-subtitle";
    subtitle.textContent =
      state.panelView === "watchdog"
        ? "查看定时任务、自动化和探针状态，并执行暂停或恢复。"
        : "按小助手、Agent 控制台和治理运营归类展示入口，顶部仅保留常用入口。";
    copy.appendChild(kicker);
    copy.appendChild(title);
    copy.appendChild(subtitle);
    var close = document.createElement("button");
    close.className = "dcqe-close";
    close.type = "button";
    close.title = "关闭";
    close.textContent = "×";
    close.addEventListener("click", function () {
      state.panelOpen = false;
      render();
    });
    head.appendChild(copy);
    head.appendChild(close);
    panel.appendChild(head);

    renderModeSwitch(panel);
    if (state.panelView === "watchdog") {
      renderWatchdogPanel(panel);
    } else {
      renderEntryDirectory(panel, entries);
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

    var summary = summarizeEntries(entries);
    var title = document.createElement("span");
    title.className = "dcqe-title";
    var titleMain = document.createElement("span");
    titleMain.className = "dcqe-title-main";
    titleMain.textContent = "系统入口";
    var titleSub = document.createElement("span");
    titleSub.className = "dcqe-title-sub";
    titleSub.textContent = state.lastError
      ? "状态读取失败"
      : summary.online + "/" + summary.total + " 在线";
    title.appendChild(titleMain);
    title.appendChild(titleSub);
    root.appendChild(title);

    var actions = document.createElement("div");
    actions.className = "dcqe-actions";
    root.appendChild(actions);

    var rail = document.createElement("div");
    rail.className = "dcqe-rail";
    primaryEntries(entries).forEach(function (entry) {
      rail.appendChild(createEntryLink(entry));
    });
    actions.appendChild(rail);

    var directoryButton = document.createElement("button");
    directoryButton.className = "dcqe-button";
    directoryButton.type = "button";
    directoryButton.dataset.active =
      state.panelOpen && state.panelView === "entries" ? "true" : "false";
    directoryButton.textContent = "全部 " + entries.length;
    directoryButton.addEventListener("click", function () {
      var alreadyOpen = state.panelOpen && state.panelView === "entries";
      state.panelOpen = !alreadyOpen;
      state.panelView = "entries";
      render();
    });
    actions.appendChild(directoryButton);

    var consoleButton = document.createElement("button");
    consoleButton.className = "dcqe-button";
    consoleButton.type = "button";
    consoleButton.dataset.active =
      state.panelOpen && state.panelView === "watchdog" ? "true" : "false";
    consoleButton.textContent = "Watch-Dog";
    consoleButton.addEventListener("click", function () {
      var alreadyOpen = state.panelOpen && state.panelView === "watchdog";
      state.panelOpen = !alreadyOpen;
      state.panelView = "watchdog";
      if (state.panelOpen && !state.watchdog) loadWatchdog("status");
      render();
    });
    actions.appendChild(consoleButton);
    renderPanel(root, entries);
  }

  function normalizePayload(payload) {
    var data = payload && payload.data ? payload.data : payload;
    if (data && data.data && Array.isArray(data.data.entries)) data = data.data;
    if (!data || !Array.isArray(data.entries)) throw new Error("entries missing");
    return mergeDefaultPinnedEntries(data.entries);
  }

  function mergeDefaultPinnedEntries(entries) {
    var merged = Array.isArray(entries) ? entries.slice() : [];
    DEFAULT_ENTRIES.forEach(function (defaultEntry) {
      if (!defaultEntry || defaultEntry.pinned !== true || !defaultEntry.url) return;
      var exists = merged.some(function (entry) {
        return (
          entry &&
          (entry.name === defaultEntry.name || entry.url === defaultEntry.url)
        );
      });
      if (!exists) {
        merged.push(defaultEntry);
      }
    });
    return sortEntries(merged.map(enrichEntry));
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

  function loadWatchdog(action, targetType, targetKey, targetRow) {
    if (action === "status") {
      state.watchdogBusy = "刷新中";
    } else if (targetKey) {
      state.watchdogBusy = (action === "pause" ? "正在暂停：" : "正在恢复：") + targetKey;
    } else {
      state.watchdogBusy = action === "pause" ? "正在暂停本组" : "正在恢复本组";
    }
    state.watchdogError = "";
    render();
    if (
      action === "pause" &&
      targetKey &&
      targetRow &&
      targetRow.impact_level === "critical"
    ) {
      state.watchdogBusy = "正在生成关键任务 Control Plan";
      render();
      requestWatchdog("plan-pause-one", state.watchdogGroup, targetType, targetKey)
        .then(function (data) {
          var plan = data && data.plan;
          if (!plan || !Array.isArray(plan.actions) || plan.actions.length !== 1) {
            throw new Error("关键任务 Control Plan 缺失");
          }
          var impact = targetRow.impact_summary || "该操作会影响系统保护能力";
          var confirmed = window.confirm(
            "关键操作：暂停 " + plan.actions[0].description + "\n\n" +
            impact +
            "\n\n计划 120 秒内有效。确认继续吗？"
          );
          if (!confirmed) return null;
          state.watchdogBusy = "正在执行已确认的关键任务 Control Plan";
          render();
          return requestWatchdog(
            action,
            state.watchdogGroup,
            targetType,
            targetKey,
            plan.plan_id
          );
        })
        .catch(function (error) {
          state.watchdogError = error && error.message ? error.message : "Control Plan 失败";
        })
        .then(function () {
          state.watchdogBusy = "";
          render();
        });
      return;
    }
    if ((action === "pause" || action === "resume") && !targetKey) {
      var operationText = action === "pause" ? "暂停" : "启动";
      var planAction = action === "pause" ? "plan-pause" : "plan-resume";
      state.watchdogBusy = "正在生成 Control Plan";
      render();
      requestWatchdog(planAction, state.watchdogGroup)
        .then(function (data) {
          var plan = data && data.plan;
          if (!plan || !Array.isArray(plan.actions)) {
            throw new Error("Control Plan 缺失");
          }
          if (plan.actions.length === 0) {
            state.watchdogError = "本组没有需要" + operationText + "的任务";
            return null;
          }
          var lines = plan.actions.map(function (item) {
            return "• " + item.description + "（" + item.schedule + "）";
          });
          var confirmed = window.confirm(
            "将" + operationText + "以下 " + plan.actions.length + " 个任务：\n\n" +
            lines.join("\n") +
            "\n\n计划 120 秒内有效。确认继续吗？"
          );
          if (!confirmed) return null;
          state.watchdogBusy = "正在执行已确认的 Control Plan";
          render();
          return requestWatchdog(
            action,
            state.watchdogGroup,
            "",
            "",
            plan.plan_id
          );
        })
        .catch(function (error) {
          state.watchdogError = error && error.message ? error.message : "Control Plan 失败";
        })
        .then(function () {
          state.watchdogBusy = "";
          render();
        });
      return;
    }
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
