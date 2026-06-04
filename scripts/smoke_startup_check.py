"""Cross-platform startup smoke check for AstrBot."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STARTUP_TIMEOUT_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 2


def _tail(path: Path, lines: int = 80) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"Unable to read smoke log: {exc}"
    return "\n".join(content[-lines:])


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _is_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=REQUEST_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _stop_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def main() -> int:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("TESTING", "true")
    port = _find_free_port()
    health_url = f"http://127.0.0.1:{port}"
    env["ASTRBOT_DASHBOARD_HOST"] = "127.0.0.1"
    env["ASTRBOT_DASHBOARD_PORT"] = str(port)

    smoke_root = Path(tempfile.mkdtemp(prefix="astrbot-smoke-root-"))
    env["ASTRBOT_ROOT"] = str(smoke_root)
    log_path = smoke_root / "smoke.log"
    webui_dir = smoke_root / "webui"
    webui_dir.mkdir()
    (webui_dir / "index.html").write_text(
        "<!doctype html><title>AstrBot</title>",
        encoding="utf-8",
    )

    with log_path.open("wb") as log_file:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(REPO_ROOT / "main.py"),
                "--webui-dir",
                str(webui_dir),
            ],
            cwd=REPO_ROOT,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
        )

    print(f"Starting smoke test on {health_url}")
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            if _is_ready("127.0.0.1", port):
                print("Smoke test passed")
                return 0

            return_code = proc.poll()
            if return_code is not None:
                print(
                    f"AstrBot exited before becoming healthy. Exit code: {return_code}",
                    file=sys.stderr,
                )
                print(_tail(log_path), file=sys.stderr)
                return 1

            time.sleep(1)

        print(
            "Smoke test failed: health endpoint did not become ready in time.",
            file=sys.stderr,
        )
        print(_tail(log_path), file=sys.stderr)
        return 1
    finally:
        _stop_process(proc)
        try:
            log_path.unlink()
        except OSError:
            pass
        shutil.rmtree(smoke_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
