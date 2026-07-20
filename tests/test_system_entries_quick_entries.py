from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _load_system_entries_module():
    module_path = Path("data/plugins/system_entries/main.py")
    spec = importlib.util.spec_from_file_location("system_entries_main", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_system_entries_merges_pinned_dashboard_entries() -> None:
    module = _load_system_entries_module()

    merged = module._merge_entries(
        [{"name": "Hermes Agent 官方 WebUI", "url": "http://localhost:9119/"}],
        module.PINNED_DASHBOARD_ENTRIES,
    )

    names = [entry["name"] for entry in merged]
    assert "OpenClaw" not in names
    assert "小助手健康" in names
    assert "记忆治理" in names
    assert "内容 SOP 运营" in names
    assert "员工需求洞察" in names
    assert "小助手实时看板" in names
    by_name = {entry["name"]: entry for entry in merged}
    assert by_name["小助手健康"]["category"] == "assistant"
    assert by_name["小助手健康"]["priority"] == 10
    default_by_name = {entry["name"]: entry for entry in module.DEFAULT_ENTRIES}
    assert default_by_name["Hermes Agent 官方 WebUI"]["category"] == "agent"


def test_system_entries_pinned_entries_are_unknown_not_ready() -> None:
    module = _load_system_entries_module()
    plugin = module.SystemEntriesPlugin.__new__(module.SystemEntriesPlugin)
    plugin.entries = module.PINNED_DASHBOARD_ENTRIES
    plugin.cache_ttl_seconds = 0
    plugin._cache = None
    plugin._cache_at = 0.0

    payload = asyncio.run(plugin._api_status())
    entries = payload["data"]["entries"]

    assert {entry["name"] for entry in entries} == {
        "小助手健康",
        "记忆治理",
        "内容 SOP 运营",
        "员工需求洞察",
        "小助手实时看板",
    }
    assert all(entry["alive"] is None for entry in entries)
    assert all(entry["availability"] == "unknown" for entry in entries)


def test_system_entries_tcp_only_service_is_not_ready() -> None:
    module = _load_system_entries_module()
    port, stop, sock, thread = _start_closing_tcp_server()
    plugin = module.SystemEntriesPlugin.__new__(module.SystemEntriesPlugin)
    plugin.entries = [
        {
            "name": "Wrong service",
            "probe_host": "127.0.0.1",
            "probe_port": port,
            "health_path": "/health",
        }
    ]
    plugin.cache_ttl_seconds = 0
    plugin._cache = None
    plugin._cache_at = 0.0

    try:
        payload = asyncio.run(plugin._api_status())
    finally:
        stop.set()
        sock.close()
        thread.join(timeout=1)

    entry = payload["data"]["entries"][0]
    assert entry["tcp_listening"] is True
    assert entry["alive"] is False
    assert entry["availability"] == "wrong_service"


def test_system_entries_http_health_service_is_ready() -> None:
    module = _load_system_entries_module()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    plugin = module.SystemEntriesPlugin.__new__(module.SystemEntriesPlugin)
    plugin.entries = [
        {
            "name": "Healthy service",
            "probe_host": "127.0.0.1",
            "probe_port": server.server_port,
            "health_path": "/health",
            "health_body_contains": "dc-agent-ok",
        }
    ]
    plugin.cache_ttl_seconds = 0
    plugin._cache = None
    plugin._cache_at = 0.0

    try:
        payload = asyncio.run(plugin._api_status())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    entry = payload["data"]["entries"][0]
    assert entry["tcp_listening"] is True
    assert entry["alive"] is True
    assert entry["availability"] == "ready"


def test_quick_entries_top_bar_allows_pinned_dashboard_entries() -> None:
    source = Path(
        "data/plugins/system_entries/dc-dashboard-quick-entries.js"
    ).read_text(encoding="utf-8")

    assert 'name: "小助手健康"' in source
    assert 'name: "记忆治理"' in source
    assert 'name: "内容 SOP 运营"' in source
    assert 'name: "员工需求洞察"' in source
    assert 'name: "小助手实时看板"' in source
    assert source.index('name: "小助手健康"') < source.index('name: "小助手实时看板"')
    assert 'url: "/#/assistant-health"' in source
    assert 'url: "/#/live-monitor"' in source
    assert 'url: "/#/memory-governance"' in source
    assert 'url: "/#/content-sop-ops"' in source
    assert 'url: "/#/employee-insight"' in source
    assert 'category: "assistant"' in source
    assert 'category: "agent"' in source
    assert 'category: "governance"' in source
    assert "entry.pinned === true" in source
    assert "mergeDefaultPinnedEntries(data.entries)" in source
    assert "DEFAULT_ENTRIES.forEach" in source
    assert 'availability === "ready"' in source
    assert 'availability === "offline"' in source
    assert 'availability === "wrong_service"' in source
    assert 'availability === "tcp_listening"' in source
    assert 'if (alive === true) return "checking"' in source


def test_quick_entries_uses_categorized_dynamic_navigation() -> None:
    source = Path(
        "data/plugins/system_entries/dc-dashboard-quick-entries.js"
    ).read_text(encoding="utf-8")

    assert (
        'var CATEGORY_ORDER = ["assistant", "agent", "governance", "automation", "other"]'
        in source
    )
    assert "function primaryEntries(entries)" in source
    assert "return entries.slice(0, 3)" in source
    assert "primaryEntries(entries).forEach" in source
    assert 'directoryButton.textContent = "全部 " + entries.length' in source
    assert "function renderEntryDirectory(panel, entries)" in source
    assert 'search.placeholder = "搜索入口、分类或说明"' in source
    assert "function renderCategoryTabs(parent, entries)" in source
    assert 'grid.className = "dcqe-panel-grid"' in source
    assert 'card.className = "dcqe-entry-card"' in source
    assert "function summarizeEntries(entries)" in source
    assert 'state.panelView === "watchdog"' in source


def test_quick_entries_watchdog_status_colors_do_not_mark_disabled_success() -> None:
    source = Path(
        "data/plugins/system_entries/dc-dashboard-quick-entries.js"
    ).read_text(encoding="utf-8")

    disabled_rule = (
        ".dcqe-pill[data-state*='disabled'],#\" +\n"
        "      ROOT_ID +\n"
        "      \" .dcqe-pill[data-state='PAUSED'],#"
    )
    assert disabled_rule in source
    assert (
        " .dcqe-pill[data-state='not-loaded']{background:#fef3c7;color:#92400e}"
        in source
    )
    assert (
        " .dcqe-pill[data-state='loaded']{background:#dcfce7;color:#166534}" in source
    )


def test_quick_entries_renders_unified_task_control_plane() -> None:
    source = Path(
        "data/plugins/system_entries/dc-dashboard-quick-entries.js"
    ).read_text(encoding="utf-8")

    assert 'renderSection(panel, "统一任务视图", controlPlane.tasks' in source
    assert 'renderSection(panel, "Watchdog 自愈策略", repair.services' in source
    assert 'renderSection(panel, "最近自愈结果", repair.recent_results' in source
    assert 'renderSection(panel, "人工介入队列", repair.reviews' in source
    assert 'requestWatchdog("plan-review"' in source
    assert '"review",\n            "watchdog",\n            "repair_review"' in source
    assert "function repairReviewAction" in source
    assert "row.notification_stages" in source
    assert 'value: "circuit_remaining_seconds"' in source
    assert 'value: "executor_role"' in source
    assert 'value: "authority_state"' in source
    assert 'retired: "已退役"' in source
    assert 'migration_required: "待迁移"' in source
    assert '"plan-resume"' in source
    assert '"plan-pause"' in source
    assert '"plan-pause-one"' in source
    assert "window.confirm" in source
    assert "plan.plan_id" in source
    assert 'targetRow.impact_level === "critical"' in source
    assert "targetRow.impact_summary" in source
    assert 'action === "pause" || action === "resume" || action === "review"' in source
    assert 'if (row.status !== "ACTIVE") return readonlyAction("不可恢复")' in source
    assert 'renderSection(panel, "Codex 重要工具"' in source
    assert '"旧 Codex 自动任务（已停用）"' in source
    assert 'return readonlyAction("不拥有")' in source
    assert '"任务控制面"' in source


def test_system_entries_group_resume_requires_activation_plan(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_system_entries_module()
    executable = tmp_path / "watchdogctl.sh"
    executable.touch()
    plugin = module.SystemEntriesPlugin.__new__(module.SystemEntriesPlugin)
    plugin.watchdogctl_path = executable
    calls: list[list[str]] = []
    plan = {
        "schema_version": 1,
        "group": "nas",
        "plan_id": "plan-123",
        "requires_confirmation": True,
        "actions": [
            {
                "task_id": "launchd:baidu-nas-sync",
                "kind": "launchd",
                "key": "baidu-nas-sync",
                "description": "百度网盘同步",
            }
        ],
        "skipped": [],
    }

    def fake_run(args, **_kwargs):
        calls.append(args)
        if args[1] == "plan-resume":
            return subprocess.CompletedProcess(args, 0, json.dumps(plan), "")
        if args[1] == "resume":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps({"group": "nas", "control_plane": {"tasks": []}}),
            "",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    preview = asyncio.run(plugin._api_watchdog(action="plan-resume", group="nas"))
    assert preview["status"] == "ok"
    assert preview["data"]["plan"] == plan
    assert {item["capability"] for item in preview["data"]["state"]["codex_tools"]} == {
        "deep_reasoning",
        "image_generation",
        "incident_diagnosis",
        "project_engineering",
    }
    assert all(
        item["owns_schedule"] is False
        for item in preview["data"]["state"]["codex_tools"]
    )
    assert calls[0][1:] == ["plan-resume", "nas", "--json"]

    missing = asyncio.run(plugin._api_watchdog(action="resume", group="nas"))
    assert missing["status"] == "error"
    assert "plan_id" in missing["message"]

    confirmed = asyncio.run(
        plugin._api_watchdog(
            action="resume",
            group="nas",
            plan_id="plan-123",
        )
    )
    assert confirmed["status"] == "ok"
    assert any(
        call[1:] == ["resume", "nas", "--confirm-plan", "plan-123"] for call in calls
    )


def test_system_entries_group_pause_requires_deactivation_plan(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_system_entries_module()
    executable = tmp_path / "watchdogctl.sh"
    executable.touch()
    plugin = module.SystemEntriesPlugin.__new__(module.SystemEntriesPlugin)
    plugin.watchdogctl_path = executable
    calls: list[list[str]] = []
    plan = {
        "schema_version": 1,
        "group": "nas",
        "operation": "pause",
        "plan_id": "pause-123",
        "requires_confirmation": True,
        "actions": [],
        "skipped": [],
    }

    def fake_run(args, **_kwargs):
        calls.append(args)
        if args[1] == "plan-pause":
            return subprocess.CompletedProcess(args, 0, json.dumps(plan), "")
        if args[1] == "pause":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps({"group": "nas", "control_plane": {"tasks": []}}),
            "",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    preview = asyncio.run(plugin._api_watchdog(action="plan-pause", group="nas"))
    assert preview["status"] == "ok"
    assert preview["data"]["plan"] == plan
    assert calls[0][1:] == ["plan-pause", "nas", "--json"]

    missing = asyncio.run(plugin._api_watchdog(action="pause", group="nas"))
    assert missing["status"] == "error"
    assert "plan_id" in missing["message"]

    confirmed = asyncio.run(
        plugin._api_watchdog(
            action="pause",
            group="nas",
            plan_id="pause-123",
        )
    )
    assert confirmed["status"] == "ok"
    assert any(
        call[1:] == ["pause", "nas", "--confirm-plan", "pause-123"] for call in calls
    )


def test_system_entries_critical_item_pause_requires_exact_plan(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_system_entries_module()
    executable = tmp_path / "watchdogctl.sh"
    executable.touch()
    plugin = module.SystemEntriesPlugin.__new__(module.SystemEntriesPlugin)
    plugin.watchdogctl_path = executable
    calls: list[list[str]] = []
    plan = {
        "schema_version": 1,
        "scope": "item",
        "operation": "pause",
        "target_kind": "cron",
        "target_key": "dc-watchdog",
        "plan_id": "item-plan-123",
        "requires_confirmation": True,
        "actions": [{"task_id": "crontab:dc-watchdog"}],
        "skipped": [],
    }

    def fake_run(args, **_kwargs):
        calls.append(args)
        if args[1] == "plan-pause-one":
            return subprocess.CompletedProcess(args, 0, json.dumps(plan), "")
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps({"group": "watchdog", "control_plane": {"tasks": []}}),
            "",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    missing = asyncio.run(
        plugin._api_watchdog(
            action="pause",
            group="watchdog",
            target_type="cron",
            target_key="dc-watchdog",
        )
    )
    assert missing["status"] == "error"
    assert calls == []

    preview = asyncio.run(
        plugin._api_watchdog(
            action="plan-pause-one",
            group="watchdog",
            target_type="cron",
            target_key="dc-watchdog",
        )
    )
    assert preview["data"]["plan"] == plan
    assert calls[0][1:] == [
        "plan-pause-one",
        "cron",
        "dc-watchdog",
        "--json",
    ]

    confirmed = asyncio.run(
        plugin._api_watchdog(
            action="pause",
            group="watchdog",
            target_type="cron",
            target_key="dc-watchdog",
            plan_id="item-plan-123",
        )
    )
    assert confirmed["status"] == "ok"
    assert any(
        call[1:]
        == [
            "pause-one",
            "cron",
            "dc-watchdog",
            "--confirm-plan",
            "item-plan-123",
        ]
        for call in calls
    )


def test_system_entries_rejects_get_mutations(tmp_path) -> None:
    module = _load_system_entries_module()
    executable = tmp_path / "watchdogctl.sh"
    executable.touch()
    plugin = module.SystemEntriesPlugin.__new__(module.SystemEntriesPlugin)
    plugin.watchdogctl_path = executable
    request = type(
        "Request",
        (),
        {
            "method": "GET",
            "query_string": "action=pause&group=onboarding&target_type=cron&target_key=onboarding-watch",
        },
    )()

    result = asyncio.run(plugin._api_watchdog(request))

    assert result["status"] == "error"
    assert result["message"] == "mutation requires POST"


def test_system_entries_repair_review_requires_exact_plan(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_system_entries_module()
    executable = tmp_path / "watchdogctl.sh"
    executable.touch()
    plugin = module.SystemEntriesPlugin.__new__(module.SystemEntriesPlugin)
    plugin.watchdogctl_path = executable
    calls: list[list[str]] = []
    plan = {
        "schema_version": 1,
        "scope": "repair_review",
        "operation": "acknowledge",
        "incident_id": "incident-9",
        "plan_id": "1000.review-plan",
        "requires_confirmation": True,
    }

    def fake_run(args, **_kwargs):
        calls.append(args)
        if args[1] == "plan-review":
            return subprocess.CompletedProcess(args, 0, json.dumps(plan), "")
        if args[1] == "review":
            return subprocess.CompletedProcess(args, 0, "{}", "")
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps({"group": "watchdog", "control_plane": {"tasks": []}}),
            "",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    preview = asyncio.run(
        plugin._api_watchdog(
            action="plan-review",
            group="watchdog",
            target_type="repair_review",
            target_key="incident-9",
            review_action="acknowledge",
        )
    )
    assert preview["data"]["plan"] == plan
    assert calls[0][1:] == [
        "plan-review",
        "incident-9",
        "acknowledge",
        "--json",
    ]

    missing = asyncio.run(
        plugin._api_watchdog(
            action="review",
            group="watchdog",
            target_type="repair_review",
            target_key="incident-9",
            review_action="acknowledge",
        )
    )
    assert missing["status"] == "error"
    assert "plan_id" in missing["message"]

    confirmed = asyncio.run(
        plugin._api_watchdog(
            action="review",
            group="watchdog",
            target_type="repair_review",
            target_key="incident-9",
            review_action="acknowledge",
            plan_id="1000.review-plan",
        )
    )
    assert confirmed["status"] == "ok"
    assert any(
        call[1:]
        == [
            "review",
            "incident-9",
            "acknowledge",
            "--confirm-plan",
            "1000.review-plan",
            "--json",
        ]
        for call in calls
    )


def test_dashboard_static_route_serves_pinned_entry_paths() -> None:
    source = Path("astrbot/dashboard/routes/static_file.py").read_text(encoding="utf-8")

    assert '"/memory-governance"' in source
    assert '"/content-sop-ops"' in source
    assert '"/employee-insight"' in source
    assert '"/assistant-health"' in source
    assert '"/live-monitor"' in source


def _start_closing_tcp_server():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    sock.settimeout(0.1)
    stop = threading.Event()

    def _serve() -> None:
        while not stop.is_set():
            try:
                conn, _addr = sock.accept()
            except OSError:
                break
            except TimeoutError:
                continue
            with conn:
                pass

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    return sock.getsockname()[1], stop, sock, thread


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"dc-agent-ok")

    def log_message(self, format, *args) -> None:
        return
