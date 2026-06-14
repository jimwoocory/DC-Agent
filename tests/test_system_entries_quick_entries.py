from __future__ import annotations

import asyncio
import importlib.util
import socket
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
        [{"name": "OpenClaw", "url": "http://localhost:4312/"}],
        module.PINNED_DASHBOARD_ENTRIES,
    )

    names = [entry["name"] for entry in merged]
    assert "OpenClaw" in names
    assert "记忆治理" in names
    assert "内容 SOP 运营" in names


def test_system_entries_pinned_entries_are_unknown_not_ready() -> None:
    module = _load_system_entries_module()
    plugin = module.SystemEntriesPlugin.__new__(module.SystemEntriesPlugin)
    plugin.entries = module.PINNED_DASHBOARD_ENTRIES
    plugin.cache_ttl_seconds = 0
    plugin._cache = None
    plugin._cache_at = 0.0

    payload = asyncio.run(plugin._api_status())
    entries = payload["data"]["entries"]

    assert {entry["name"] for entry in entries} == {"记忆治理", "内容 SOP 运营"}
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

    assert 'name: "记忆治理"' in source
    assert 'name: "内容 SOP 运营"' in source
    assert 'url: "/#/memory-governance"' in source
    assert 'url: "/#/content-sop-ops"' in source
    assert "entry.pinned === true" in source
    assert 'availability === "ready"' in source
    assert 'availability === "offline"' in source
    assert 'availability === "wrong_service"' in source
    assert 'availability === "tcp_listening"' in source
    assert 'if (alive === true) return "checking"' in source


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


def test_dashboard_static_route_serves_pinned_entry_paths() -> None:
    source = Path("astrbot/dashboard/routes/static_file.py").read_text(encoding="utf-8")

    assert '"/memory-governance"' in source
    assert '"/content-sop-ops"' in source


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
