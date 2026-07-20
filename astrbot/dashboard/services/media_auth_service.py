"""NAS-local OAuth Device Flow management for Image2 and Dreamina."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dc_engines.dreamina_cli import build_dreamina_subprocess_env

MediaProvider = Literal["codex", "dreamina"]
_TOKEN_RE = re.compile(
    r'(?i)("?(?:access_token|refresh_token|id_token)"?\s*[:=]\s*["\']?)[^\s,"\']+'
)
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_VERIFICATION_URI_RE = re.compile(
    r"(?im)^\s*verification_uri\s*:\s*(https?://[^\s<>\"']+)"
)
_PLAIN_USER_CODE_RE = re.compile(r"(?im)^\s*user_code\s*:\s*([A-Za-z0-9._~-]+)")
_DEVICE_CODE_RE = re.compile(r"(?im)^\s*device_code\s*:\s*([A-Za-z0-9._~-]+)")
_USER_CODE_RE = re.compile(
    r"(?is)(?:user[_\s-]*code|one[-\s]*time code|enter code|授权码|验证码)"
    r".{0,120}?([A-Z0-9]{4,}-[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})?)"
)


@dataclass(slots=True)
class CommandResult:
    """Captured subprocess result.

    Args:
        returncode: Process return code, or ``None`` after a timeout.
        stdout: Captured standard output.
        stderr: Captured standard error.
        timed_out: Whether the process exceeded the configured timeout.
    """

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(slots=True)
class CodexLoginSession:
    """Live Codex device-auth subprocess.

    Args:
        session_id: Random identifier returned to the dashboard.
        process: Active Codex CLI process.
        output: Captured process output lines.
        readers: Stream reader tasks retained until process completion.
        created_at: Monotonic creation timestamp.
    """

    session_id: str
    process: asyncio.subprocess.Process
    output: list[str] = field(default_factory=list)
    readers: list[asyncio.Task[None]] = field(default_factory=list)
    created_at: float = field(default_factory=time.monotonic)


class MediaAuthService:
    """Manage NAS-local media provider authentication without exposing tokens."""

    def __init__(self) -> None:
        self._codex_session: CodexLoginSession | None = None
        self._session_lock = asyncio.Lock()

    @staticmethod
    def _environment() -> dict[str, str]:
        """Build the inherited proxy-aware subprocess environment.

        Returns:
            Environment used by Codex and Dreamina commands.
        """
        return build_dreamina_subprocess_env(dict(os.environ))

    @staticmethod
    def _executable(provider: MediaProvider) -> str | None:
        """Resolve a provider CLI executable.

        Args:
            provider: Codex or Dreamina.

        Returns:
            Executable path when installed.
        """
        override_name = "CODEX_CLI_PATH" if provider == "codex" else "DREAMINA_CLI_PATH"
        override = str(os.environ.get(override_name, "")).strip()
        if override:
            path = Path(override).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        return shutil.which("codex" if provider == "codex" else "dreamina")

    @staticmethod
    def _safe_output(text: str, limit: int = 500) -> str:
        """Redact token-shaped fields and bound provider output.

        Args:
            text: Raw CLI output.
            limit: Maximum returned characters.

        Returns:
            Single-line redacted diagnostic text.
        """
        redacted = _ANSI_RE.sub("", str(text or ""))
        redacted = _TOKEN_RE.sub(r"\1[REDACTED]", redacted)
        redacted = re.sub(
            r"(?i)bearer\s+[A-Za-z0-9._~-]+", "Bearer [REDACTED]", redacted
        )
        return " ".join(redacted.split())[:limit]

    @staticmethod
    def _proxy_state() -> dict[str, object]:
        """Return a non-secret summary of the inherited proxy route.

        Returns:
            Proxy configured flag and sanitized host label.
        """
        raw = str(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "")
        parsed = urlsplit(raw)
        label = parsed.hostname or ""
        if parsed.port:
            label = f"{label}:{parsed.port}"
        return {"configured": bool(label), "route": label}

    async def _run(
        self,
        args: list[str],
        *,
        timeout: float,
    ) -> CommandResult:
        """Run one bounded provider CLI command.

        Args:
            args: Executable and arguments.
            timeout: Maximum runtime in seconds.

        Returns:
            Captured subprocess result.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._environment(),
            )
        except OSError as exc:
            return CommandResult(127, "", str(exc))
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return CommandResult(None, "", "command timed out", timed_out=True)
        return CommandResult(
            process.returncode,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    @staticmethod
    async def _capture_stream(
        stream: asyncio.StreamReader | None,
        output: list[str],
    ) -> None:
        """Append process output lines while a device flow is active.

        Args:
            stream: Process stdout or stderr stream.
            output: Shared bounded output list.
        """
        if stream is None:
            return
        while line := await stream.readline():
            output.append(line.decode("utf-8", errors="replace"))
            if len(output) > 200:
                del output[:-200]

    @staticmethod
    def _json_values(text: str) -> list[object]:
        """Extract JSON values embedded in mixed CLI output.

        Args:
            text: Raw command output.

        Returns:
            Parsed JSON values.
        """
        values: list[object] = []
        decoder = json.JSONDecoder()
        index = 0
        while index < len(text):
            positions = [
                position
                for position in (text.find("{", index), text.find("[", index))
                if position >= 0
            ]
            if not positions:
                break
            start = min(positions)
            try:
                value, consumed = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                index = start + 1
                continue
            values.append(value)
            index = start + consumed
        return values

    @classmethod
    def _find_json_key(cls, text: str, key: str) -> str:
        """Find a scalar key inside embedded JSON.

        Args:
            text: Raw provider output.
            key: Key to locate recursively.

        Returns:
            String value or an empty string.
        """
        queue = cls._json_values(text)
        while queue:
            value = queue.pop(0)
            if isinstance(value, dict):
                if key in value and value[key] is not None:
                    return str(value[key])
                queue.extend(value.values())
            elif isinstance(value, list):
                queue.extend(value)
        return ""

    @classmethod
    def _authorization_material(cls, text: str) -> dict[str, str]:
        """Parse verification URL and codes from provider CLI output.

        Args:
            text: Raw Device Flow output.

        Returns:
            Parsed public authorization material.
        """
        clean_text = _ANSI_RE.sub("", text)
        verification_url = cls._find_json_key(
            clean_text, "verification_uri_complete"
        ) or cls._find_json_key(clean_text, "verification_uri")
        if not verification_url and (match := _VERIFICATION_URI_RE.search(clean_text)):
            verification_url = match.group(1)
        if not verification_url:
            for candidate in _URL_RE.findall(clean_text):
                if "device" in candidate.lower() or "auth" in candidate.lower():
                    verification_url = candidate.rstrip(".,;)")
                    break
        user_code = cls._find_json_key(clean_text, "user_code")
        if not user_code and (match := _PLAIN_USER_CODE_RE.search(clean_text)):
            user_code = match.group(1)
        elif not user_code and (match := _USER_CODE_RE.search(clean_text)):
            user_code = match.group(1)
        device_code = cls._find_json_key(clean_text, "device_code")
        if not device_code and (match := _DEVICE_CODE_RE.search(clean_text)):
            device_code = match.group(1)
        return {
            "verification_url": verification_url,
            "user_code": user_code,
            "device_code": device_code,
        }

    @staticmethod
    def _codex_token_expiry() -> tuple[bool, str]:
        """Read Codex token presence and JWT expiry without returning claims.

        Returns:
            Token presence and UTC expiry timestamp.
        """
        auth_path = Path.home() / ".codex" / "auth.json"
        try:
            payload = json.loads(auth_path.read_text(encoding="utf-8"))
            token = payload.get("tokens", {}).get("access_token", "")
            parts = str(token).split(".")
            if len(parts) < 2:
                return bool(token), ""
            encoded = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(encoded))
            expires_at = int(claims.get("exp") or 0)
            if not expires_at:
                return True, ""
            return True, datetime.fromtimestamp(expires_at, UTC).isoformat()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False, ""

    async def _codex_status(self) -> dict[str, object]:
        """Return redacted Codex OAuth state.

        Returns:
            Image2 authentication status.
        """
        executable = self._executable("codex")
        token_present, expires_at = self._codex_token_expiry()
        authenticated = token_present
        detail = (
            "Codex OAuth 已写入 NAS 持久化卷" if token_present else "未找到 Codex OAuth"
        )
        if executable:
            result = await self._run([executable, "login", "status"], timeout=12)
            output = f"{result.stdout}\n{result.stderr}"
            authenticated = result.returncode == 0 and "logged in" in output.lower()
            if authenticated:
                detail = "Codex OAuth 登录有效"
            elif token_present:
                detail = self._safe_output(output) or "Codex OAuth 需要重新登录"
        if expires_at:
            try:
                expired = datetime.fromisoformat(expires_at) <= datetime.now(UTC)
            except ValueError:
                expired = False
            if expired:
                authenticated = False
                detail = "Codex access token 已过期，请重新登录"
        return {
            "provider": "codex",
            "label": "Image2 · Codex OAuth",
            "executable_available": bool(executable),
            "credential_present": token_present,
            "authenticated": authenticated,
            "expires_at": expires_at,
            "detail": detail,
            "proxy": self._proxy_state(),
        }

    async def _dreamina_status(self) -> dict[str, object]:
        """Return redacted Dreamina login and credit state.

        Returns:
            Dreamina authentication status.
        """
        executable = self._executable("dreamina")
        if not executable:
            return {
                "provider": "dreamina",
                "label": "即梦 OAuth",
                "executable_available": False,
                "credential_present": False,
                "authenticated": False,
                "credits": "",
                "detail": "NAS 未安装 dreamina CLI",
                "proxy": self._proxy_state(),
            }
        result = await self._run([executable, "user_credit"], timeout=20)
        output = f"{result.stdout}\n{result.stderr}"
        credits = self._find_json_key(output, "total_credit") or self._find_json_key(
            output, "credit"
        )
        authenticated = result.returncode == 0
        if authenticated:
            detail = "即梦 OAuth 登录有效"
        else:
            detail = "即梦未登录或登录已过期"
            if result.timed_out:
                detail = "即梦登录检测超时"
        return {
            "provider": "dreamina",
            "label": "即梦 OAuth",
            "executable_available": True,
            "credential_present": authenticated,
            "authenticated": authenticated,
            "credits": credits,
            "detail": detail,
            "proxy": self._proxy_state(),
        }

    async def status(self) -> dict[str, object]:
        """Return both media provider states concurrently.

        Returns:
            Provider state list and NAS runtime marker.
        """
        codex, dreamina = await asyncio.gather(
            self._codex_status(), self._dreamina_status()
        )
        return {"runtime": "NAS", "providers": [codex, dreamina]}

    async def _stop_codex_session(self) -> None:
        """Terminate any prior Codex device-auth process."""
        session = self._codex_session
        if session is None:
            return
        if session.process.returncode is None:
            session.process.terminate()
            try:
                await asyncio.wait_for(session.process.wait(), timeout=3)
            except TimeoutError:
                session.process.kill()
                await session.process.wait()
        if session.readers:
            await asyncio.gather(*session.readers, return_exceptions=True)
        self._codex_session = None

    async def _start_codex_login(self) -> dict[str, object]:
        """Start a retained Codex device-auth process.

        Returns:
            Public authorization material and opaque session id.

        Raises:
            RuntimeError: If Codex is unavailable or emits no authorization code.
        """
        executable = self._executable("codex")
        if not executable:
            raise RuntimeError("NAS 未安装 Codex CLI，暂时无法发起 Image2 登录")
        async with self._session_lock:
            await self._stop_codex_session()
            process = await asyncio.create_subprocess_exec(
                executable,
                "login",
                "--device-auth",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._environment(),
            )
            session = CodexLoginSession(
                session_id=secrets.token_urlsafe(24),
                process=process,
            )
            session.readers = [
                asyncio.create_task(
                    self._capture_stream(process.stdout, session.output)
                ),
                asyncio.create_task(
                    self._capture_stream(process.stderr, session.output)
                ),
            ]
            self._codex_session = session
        material: dict[str, str] = {}
        for _ in range(100):
            material = self._authorization_material("".join(session.output))
            if material["verification_url"] and material["user_code"]:
                break
            if process.returncode is not None:
                break
            await asyncio.sleep(0.1)
        if not material.get("verification_url") or not material.get("user_code"):
            diagnostic = self._safe_output("".join(session.output))
            await self._stop_codex_session()
            raise RuntimeError(diagnostic or "Codex 没有返回设备授权码")
        return {
            "provider": "codex",
            "state": "pending",
            "session_id": session.session_id,
            "verification_url": material["verification_url"],
            "user_code": material["user_code"],
        }

    async def _start_dreamina_login(self) -> dict[str, object]:
        """Start Dreamina headless Device Flow.

        Returns:
            Public authorization material and device code.

        Raises:
            RuntimeError: If Dreamina is unavailable or login cannot start.
        """
        executable = self._executable("dreamina")
        if not executable:
            raise RuntimeError("NAS 未安装 dreamina CLI，暂时无法发起即梦登录")
        result = await self._run([executable, "login", "--headless"], timeout=60)
        output = f"{result.stdout}\n{result.stderr}"
        material = self._authorization_material(output)
        if (
            result.returncode != 0
            or not material["verification_url"]
            or not material["user_code"]
            or not material["device_code"]
        ):
            raise RuntimeError(self._safe_output(output) or "即梦没有返回设备授权码")
        return {
            "provider": "dreamina",
            "state": "pending",
            **material,
        }

    async def start_login(self, provider: MediaProvider) -> dict[str, object]:
        """Start the selected provider Device Flow.

        Args:
            provider: Codex or Dreamina.

        Returns:
            Provider-specific public authorization material.

        Raises:
            ValueError: If the provider is unsupported.
        """
        if provider == "codex":
            return await self._start_codex_login()
        if provider == "dreamina":
            return await self._start_dreamina_login()
        raise ValueError("不支持的媒体登录提供商")

    async def _check_codex_login(self, session_id: str) -> dict[str, object]:
        """Poll a retained Codex login process.

        Args:
            session_id: Opaque server session id.

        Returns:
            Pending, authenticated, or failed state.

        Raises:
            ValueError: If the session id is missing or stale.
        """
        session = self._codex_session
        if not session_id or session is None or session.session_id != session_id:
            raise ValueError("Codex 登录会话不存在或已经失效")
        material = self._authorization_material("".join(session.output))
        if session.process.returncode is None:
            return {
                "provider": "codex",
                "state": "pending",
                "session_id": session.session_id,
                "verification_url": material["verification_url"],
                "user_code": material["user_code"],
            }
        await asyncio.gather(*session.readers, return_exceptions=True)
        status = await self._codex_status()
        state = "authenticated" if status["authenticated"] else "failed"
        if state == "failed":
            status["detail"] = (
                self._safe_output("".join(session.output)) or status["detail"]
            )
        self._codex_session = None
        return {"provider": "codex", "state": state, **status}

    async def _check_dreamina_login(
        self,
        device_code: str,
        poll_seconds: int,
    ) -> dict[str, object]:
        """Poll Dreamina headless Device Flow.

        Args:
            device_code: Device code returned by login start.
            poll_seconds: Bounded upstream polling duration.

        Returns:
            Pending, authenticated, or failed state.

        Raises:
            ValueError: If the device code is missing.
            RuntimeError: If Dreamina CLI is unavailable.
        """
        if not device_code:
            raise ValueError("即梦登录缺少 device_code")
        executable = self._executable("dreamina")
        if not executable:
            raise RuntimeError("NAS 未安装 dreamina CLI")
        result = await self._run(
            [
                executable,
                "login",
                "checklogin",
                "--device_code",
                device_code,
                "--poll",
                str(max(0, min(poll_seconds, 30))),
            ],
            timeout=max(45, poll_seconds + 15),
        )
        output = f"{result.stdout}\n{result.stderr}"
        if result.returncode == 0:
            status = await self._dreamina_status()
            return {"provider": "dreamina", "state": "authenticated", **status}
        lowered = output.lower()
        if not output.strip() or any(
            marker in lowered
            for marker in ("authorization_pending", "pending", "等待", "尚未完成")
        ):
            return {
                "provider": "dreamina",
                "state": "pending",
                "detail": "等待即梦授权完成",
            }
        return {
            "provider": "dreamina",
            "state": "failed",
            "detail": self._safe_output(output) or "即梦登录失败",
        }

    async def check_login(
        self,
        provider: MediaProvider,
        *,
        session_id: str = "",
        device_code: str = "",
        poll_seconds: int = 0,
    ) -> dict[str, object]:
        """Poll a provider Device Flow.

        Args:
            provider: Codex or Dreamina.
            session_id: Codex server session identifier.
            device_code: Dreamina device code.
            poll_seconds: Bounded Dreamina polling duration.

        Returns:
            Provider login state.

        Raises:
            ValueError: If the provider is unsupported or state is incomplete.
        """
        if provider == "codex":
            return await self._check_codex_login(session_id)
        if provider == "dreamina":
            return await self._check_dreamina_login(device_code, poll_seconds)
        raise ValueError("不支持的媒体登录提供商")

    async def test(self, provider: MediaProvider) -> dict[str, object]:
        """Run a non-generating authentication probe.

        Args:
            provider: Codex or Dreamina.

        Returns:
            Provider state with a test marker.

        Raises:
            ValueError: If the provider is unsupported.
        """
        if provider == "codex":
            status = await self._codex_status()
        elif provider == "dreamina":
            status = await self._dreamina_status()
        else:
            raise ValueError("不支持的媒体登录提供商")
        return {**status, "tested": True}

    async def logout(self, provider: MediaProvider) -> dict[str, object]:
        """Remove one provider login through its CLI.

        Args:
            provider: Codex or Dreamina.

        Returns:
            Logged-out provider state.

        Raises:
            RuntimeError: If the CLI is unavailable or logout fails.
        """
        executable = self._executable(provider)
        if not executable:
            raise RuntimeError(f"NAS 未安装 {provider} CLI")
        if provider == "codex":
            await self._stop_codex_session()
        result = await self._run([executable, "logout"], timeout=30)
        if result.returncode != 0:
            output = f"{result.stdout}\n{result.stderr}"
            raise RuntimeError(self._safe_output(output) or f"{provider} 退出登录失败")
        status = (
            await self._codex_status()
            if provider == "codex"
            else await self._dreamina_status()
        )
        return {**status, "authenticated": False, "credential_present": False}


__all__ = ["CommandResult", "MediaAuthService"]
