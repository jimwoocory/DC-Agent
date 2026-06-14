"""OpenClaw Control Center on-demand 看门狗。

让 OpenClaw（一个偶尔才看的可视化插件）变成"按需启 + 空闲自停"：

1. 起一个轻量 aiohttp server 在 :9120，暴露 ``GET /kick``
2. dashboard 顶部"OpenClaw" 按钮 href 改成 ``http://localhost:9120/kick``
3. 点按钮 → kick 端点 → 没起就 spawn npm run dev:ui → 等 :4312 ready → 302 跳 4312
4. background task 每 60s 检查最后一次 kick 时间，超 ``idle_timeout_seconds`` 自动 kill OpenClaw
5. AstrBot 重启时 plugin 重 init，**接管**已经在跑的 OpenClaw（读 PID 文件），不重启

故意不调 ``launchctl``——OpenClaw 是可视化插件，不是 daemon。
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

from aiohttp import web

from astrbot.api import logger
from astrbot.api.star import Context, Star, register


@register(
    "openclaw_on_demand",
    "dc_agent",
    "OpenClaw Control Center 按需启 + 空闲自停（看门狗 :9120）",
    "1.0.0",
)
class OpenClawOnDemandPlugin(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        cfg = config or {}
        self.openclaw_cwd: Path = Path(
            cfg.get("openclaw_cwd", "/Users/dianchi/DC-Agent/openclaw-control-center")
        )
        self.openclaw_port: int = int(cfg.get("openclaw_port", 4312))
        self.kick_port: int = int(cfg.get("kick_port", 9120))
        self.idle_timeout_seconds: int = int(cfg.get("idle_timeout_seconds", 7200))
        self.startup_wait_seconds: int = int(cfg.get("startup_wait_seconds", 30))
        self.openclaw_health_path: str = str(cfg.get("openclaw_health_path", "/"))
        self.openclaw_health_body_contains: str = str(
            cfg.get("openclaw_health_body_contains", "OpenClaw")
        )
        self.npm_cmd: list[str] = list(cfg.get("npm_cmd", ["npm", "run", "dev:ui"]))
        self.log_dir: Path = Path(
            cfg.get("log_dir", "/Users/dianchi/DC-Agent/logs/openclaw-control-center")
        )

        self._openclaw_pid: int | None = None
        self._last_kick_at: float = 0.0
        self._lock = asyncio.Lock()
        self._runner: web.AppRunner | None = None
        self._idle_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        # 接管：如果 4312 已经在 listen，读 PID 文件登记，不重启
        if self._openclaw_ready():
            existing_pid = self._read_pid_file()
            if existing_pid and self._pid_alive(existing_pid):
                self._openclaw_pid = existing_pid
                self._last_kick_at = time.time()
                logger.info(
                    "[openclaw_on_demand] 接管已运行 OpenClaw PID=%s（idle 从现在计时）",
                    existing_pid,
                )
        elif self._port_in_use(self.openclaw_port):
            logger.warning(
                "[openclaw_on_demand] :%s 已监听但不是 OpenClaw health 响应，"
                "不会接管或跳转。",
                self.openclaw_port,
            )

        # 起 kick aiohttp server
        app = web.Application()
        app.router.add_get("/kick", self._handle_kick)
        app.router.add_get("/status", self._handle_status)
        app.router.add_get("/stop", self._handle_stop)

        runner = web.AppRunner(app)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", self.kick_port).start()
        except OSError as exc:
            logger.warning(
                "[openclaw_on_demand] 端口 %s 占用，看门狗启不了：%s",
                self.kick_port,
                exc,
            )
            return
        self._runner = runner

        # 起 idle watchdog
        self._idle_task = asyncio.create_task(self._idle_loop())

        logger.info(
            "[openclaw_on_demand] 启动 kick:127.0.0.1:%s "
            "→ openclaw:%s · idle_timeout=%ds",
            self.kick_port,
            self.openclaw_port,
            self.idle_timeout_seconds,
        )

    # ─────────────────────── HTTP handlers ───────────────────────

    async def _handle_kick(self, request: web.Request) -> web.Response:
        """点 dashboard 按钮 → 这里 → 启 OpenClaw（如未起）→ 302 跳 4312。"""
        async with self._lock:
            port_listening = self._port_in_use(self.openclaw_port)
            if port_listening and not self._openclaw_ready():
                return web.Response(
                    text=(
                        f"OpenClaw 服务不匹配：:{self.openclaw_port} 有进程监听，"
                        "但 health 探针未通过。请检查端口占用。"
                    ),
                    status=503,
                )
            if not port_listening:
                logger.info("[openclaw_on_demand] kick 触发启动 OpenClaw")
                pid = self._spawn_openclaw()
                if pid is None:
                    return web.Response(
                        text=f"启动失败，看 {self.log_dir}",
                        status=500,
                    )
                # 等 :4312 ready（最多 startup_wait_seconds 秒，每 0.5s 探一次）
                for _ in range(self.startup_wait_seconds * 2):
                    await asyncio.sleep(0.5)
                    if self._openclaw_ready():
                        break
                else:
                    return web.Response(
                        text=(
                            f"OpenClaw 启动超时（{self.startup_wait_seconds}s 内 health 未通过）。"
                            f"看 {self.log_dir / 'control-center.err.log'}"
                        ),
                        status=504,
                    )
            self._last_kick_at = time.time()

        raise web.HTTPFound(f"http://localhost:{self.openclaw_port}/")

    async def _handle_status(self, request: web.Request) -> web.Response:
        """GET /status — 看门狗状态 JSON。"""
        openclaw_listening = self._port_in_use(self.openclaw_port)
        openclaw_ready = self._openclaw_ready() if openclaw_listening else False
        availability = (
            "ready"
            if openclaw_ready
            else "wrong_service"
            if openclaw_listening
            else "offline"
        )
        return web.json_response(
            {
                "kick_port": self.kick_port,
                "openclaw_port": self.openclaw_port,
                "openclaw_pid": self._openclaw_pid,
                "openclaw_listening": openclaw_listening,
                "openclaw_ready": openclaw_ready,
                "availability": availability,
                "last_kick_at_unix": self._last_kick_at,
                "idle_seconds": (
                    int(time.time() - self._last_kick_at)
                    if self._last_kick_at
                    else None
                ),
                "idle_timeout_seconds": self.idle_timeout_seconds,
            }
        )

    async def _handle_stop(self, request: web.Request) -> web.Response:
        """GET /stop — 手动停 OpenClaw（不停看门狗自己）。"""
        async with self._lock:
            killed = self._kill_openclaw()
        return web.json_response({"killed": killed})

    # ─────────────────────── helpers ───────────────────────

    def _spawn_openclaw(self) -> int | None:
        if not self.openclaw_cwd.is_dir():
            logger.warning(
                "[openclaw_on_demand] OpenClaw cwd 不存在：%s", self.openclaw_cwd
            )
            return None
        self.log_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.log_dir / "control-center.out.log"
        err_path = self.log_dir / "control-center.err.log"
        try:
            out_f = open(out_path, "a", encoding="utf-8")
            err_f = open(err_path, "a", encoding="utf-8")
            proc = subprocess.Popen(
                self.npm_cmd,
                cwd=str(self.openclaw_cwd),
                stdout=out_f,
                stderr=err_f,
                start_new_session=True,  # 脱离 plugin 进程组，AstrBot 退出不影响它
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[openclaw_on_demand] spawn 失败：%s", exc)
            return None
        self._openclaw_pid = proc.pid
        self._write_pid_file(proc.pid)
        return proc.pid

    def _kill_openclaw(self) -> bool:
        pid = self._openclaw_pid or self._read_pid_file()
        if not pid or not self._pid_alive(pid):
            self._openclaw_pid = None
            return False
        try:
            # kill 整个进程组（npm run dev:ui 会 fork 子进程 node + esbuild）
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:  # noqa: BLE001
                pass
        self._openclaw_pid = None
        try:
            (self.log_dir / "control-center.pid").unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        logger.info("[openclaw_on_demand] killed OpenClaw PID=%s（idle 超时）", pid)
        return True

    async def _idle_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                if not self._openclaw_pid or not self._pid_alive(self._openclaw_pid):
                    continue
                if not self._last_kick_at:
                    continue
                idle = time.time() - self._last_kick_at
                if idle < self.idle_timeout_seconds:
                    continue
                async with self._lock:
                    self._kill_openclaw()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.debug("[openclaw_on_demand] idle loop 异常：%s", exc)

    def _port_in_use(self, port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(("127.0.0.1", port))
                return True
        except (ConnectionRefusedError, OSError, TimeoutError):
            return False

    def _openclaw_ready(self) -> bool:
        if not self._port_in_use(self.openclaw_port):
            return False
        return _http_health_ready(
            "127.0.0.1",
            self.openclaw_port,
            self.openclaw_health_path,
            self.openclaw_health_body_contains,
        )

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def _read_pid_file(self) -> int | None:
        pid_file = self.log_dir / "control-center.pid"
        if not pid_file.exists():
            return None
        try:
            return int(pid_file.read_text().strip())
        except (ValueError, OSError):
            return None

    def _write_pid_file(self, pid: int) -> None:
        try:
            (self.log_dir / "control-center.pid").write_text(str(pid))
        except OSError as exc:
            logger.debug("[openclaw_on_demand] 写 PID 文件失败：%s", exc)


def _http_health_ready(
    host: str,
    port: int,
    path: str,
    body_contains: str = "",
) -> bool:
    try:
        if not path.startswith("/"):
            path = f"/{path}"
        with socket.create_connection((host, port), timeout=0.75) as sock:
            sock.settimeout(0.75)
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "User-Agent: DC-Agent-openclaw-health\r\n"
                "Connection: close\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
            payload = _recv_http_response(sock)
        header, _, body = payload.partition(b"\r\n\r\n")
        status_line = header.splitlines()[0].decode("ascii", errors="ignore")
        parts = status_line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            return False
        status = int(parts[1])
        if not 200 <= status < 400:
            return False
        if body_contains:
            return body_contains in body.decode("utf-8", errors="ignore")
        return True
    except Exception:
        return False


def _recv_http_response(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < 65536:
        chunk = sock.recv(min(4096, 65536 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)
