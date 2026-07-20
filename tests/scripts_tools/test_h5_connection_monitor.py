from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

SCRIPT = Path("scripts-watchdog/h5_connection_monitor.py")
SPEC = importlib.util.spec_from_file_location("h5_connection_monitor", SCRIPT)
assert SPEC and SPEC.loader
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


def test_collect_connections_records_only_safe_peer_metadata(monkeypatch) -> None:
    output = "\n".join(
        [
            "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME",
            "python 1 dianchi 48u IPv4 0 0t0 TCP 192.168.2.162:6185->192.168.1.88:53124 (ESTABLISHED)",
            "python 1 dianchi 49u IPv4 0 0t0 TCP 127.0.0.1:6185->127.0.0.1:53125 (ESTABLISHED)",
        ]
    )
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, output, ""),
    )

    records = monitor.collect_connections(6185)

    assert len(records) == 1
    assert records[0] == {
        "observed_at": records[0]["observed_at"],
        "source_ip": "192.168.1.88",
        "source_port": 53124,
        "local_port": 6185,
        "tcp_state": "ESTABLISHED",
    }
    assert set(records[0]) == {
        "observed_at",
        "source_ip",
        "source_port",
        "local_port",
        "tcp_state",
    }


def test_run_monitor_writes_jsonl_without_application_data(
    monkeypatch, tmp_path: Path
) -> None:
    record = {
        "observed_at": "2026-07-15T04:00:00Z",
        "source_ip": "192.168.1.88",
        "source_port": 53124,
        "local_port": 6185,
        "tcp_state": "ESTABLISHED",
    }
    monkeypatch.setattr(monitor, "collect_connections", lambda port: [record])
    history_path = tmp_path / "history.jsonl"

    assert monitor.run_monitor(6185, 0.5, history_path, once=True) == 0

    written = json.loads(history_path.read_text(encoding="utf-8"))
    assert written == record
    assert history_path.stat().st_mode & 0o777 == 0o600
    assert not {
        "url",
        "query",
        "request_body",
        "capability_token",
        "quotation_data",
    }.intersection(written)


def test_launchagent_installer_keeps_monitor_running() -> None:
    installer = Path("scripts-tools/install-h5-connection-monitor.sh").read_text(
        encoding="utf-8"
    )

    assert "com.dcagent.h5-connection-monitor" in installer
    assert "<key>KeepAlive</key>" in installer
    assert "h5_connection_monitor.py" in installer
    assert "--port" in installer and "6185" in installer
