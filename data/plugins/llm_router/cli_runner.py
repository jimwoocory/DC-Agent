"""Controlled subprocess runner for CLI-backed LLM providers."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import pexpect

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")
AGY_AUTH_PATTERNS = (
    "Authentication required",
    "authorization code",
    "Visit the URL",
    "oauth2/auth",
    "Please visit",
)


@dataclass(frozen=True, slots=True)
class CliResult:
    text: str = ""
    raw: dict = field(default_factory=dict)
    model_usage: dict = field(default_factory=dict)
    elapsed_sec: float = 0.0
    error_code: str | None = None
    error: str | None = None
    exit_code: int | None = None
    command_preview: str = ""

    @property
    def ok(self) -> bool:
        return self.error_code is None and bool(self.text.strip())


class CliRunner:
    """Run approved local AI CLIs without invoking a shell."""

    ALLOWED_BINS = {
        "Antigravity",
        "Claude",
        "agy",
        "antigravity",
        "claude",
        "codex",
        "gemini",
        "grok",
    }

    def __init__(self, cwd: Path | None = None) -> None:
        self.cwd = cwd or Path(tempfile.gettempdir())

    def _default_claude_bin(self) -> str:
        return "Claude" if shutil.which("Claude") else "claude"

    async def _kill_process_tree(self, proc) -> None:
        """Best-effort kill for CLI processes and their children."""
        try:
            pid = getattr(proc, "pid", None)
            if pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:  # noqa: BLE001
            pass

    async def run_gemini(
        self,
        prompt: str,
        *,
        model: str = "gemini-3.1-pro-preview",
        timeout: float = 300,
    ) -> CliResult:
        args = [
            "gemini",
            "-p",
            prompt,
            "--model",
            model,
            "--output-format",
            "json",
            "--skip-trust",
            "--approval-mode",
            "plan",
        ]
        return await self._run_json_cli(
            args,
            timeout=timeout,
            text_field="response",
        )

    async def run_antigravity(
        self,
        prompt: str,
        *,
        model: str = "gemini-3.5-flash",
        timeout: float = 90,
        attachment_paths: list[str] | None = None,
    ) -> CliResult:
        """Run Antigravity CLI via agy print mode."""
        bin_path = os.environ.get("DC_ANTIGRAVITY_CLI_BIN", "agy").strip()
        try:
            max_timeout = float(os.environ.get("DC_ANTIGRAVITY_MAX_TIMEOUT", "90"))
        except ValueError:
            max_timeout = 90.0
        effective_timeout = min(float(timeout), max_timeout)
        full_prompt = prompt
        if attachment_paths:
            refs = "\n".join(f"- {path}" for path in attachment_paths if path)
            if refs:
                full_prompt = (
                    f"{prompt}\n\n<local_attachments>\n{refs}\n</local_attachments>"
                )

        args_template = os.environ.get("DC_ANTIGRAVITY_CLI_ARGS")
        if args_template:
            try:
                extra_args = [
                    part.format(
                        model=model,
                        prompt=full_prompt,
                        timeout_seconds=int(effective_timeout),
                    )
                    for part in shlex.split(args_template)
                    if part.strip()
                ]
            except ValueError as exc:
                return CliResult(
                    elapsed_sec=0.0,
                    error_code="bad_cli_args",
                    error=f"Invalid DC_ANTIGRAVITY_CLI_ARGS: {exc}",
                    command_preview=bin_path,
                )
            args = [bin_path, *extra_args]
            if "{prompt}" not in args_template:
                args.append(full_prompt)
            return await self._run_stdout_cli(
                args,
                prompt="",
                timeout=effective_timeout,
                text_fields=("response", "result", "text", "content"),
            )
        else:
            with tempfile.NamedTemporaryFile(
                mode="w+",
                suffix=".log",
                encoding="utf-8",
                delete=True,
            ) as log_file:
                return await asyncio.to_thread(
                    self._run_antigravity_pty,
                    bin_path,
                    [
                        "--log-file",
                        log_file.name,
                        "--print",
                        full_prompt,
                        "--dangerously-skip-permissions",
                        "--print-timeout",
                        f"{int(effective_timeout)}s",
                    ],
                    effective_timeout,
                    Path(log_file.name),
                )

    def _classify_antigravity_failure(self, output: str) -> tuple[str | None, str]:
        if any(pattern in output for pattern in AGY_AUTH_PATTERNS):
            return "auth_required", "agy requires OAuth authorization"
        if (
            "User location is not supported" in output
            or "not eligible for Antigravity" in output
        ):
            return "unsupported_location", "agy account/location is not eligible"
        return None, ""

    def _run_antigravity_pty(
        self,
        bin_path: str,
        args: list[str],
        timeout: float,
        log_path: Path | None = None,
    ) -> CliResult:
        started_at = time.perf_counter()
        command_preview = shlex.join([bin_path, *args])
        bin_name = Path(bin_path).name
        if bin_name not in self.ALLOWED_BINS:
            return CliResult(
                elapsed_sec=0.0,
                error_code="bin_not_allowed",
                error=f"CLI binary is not allowed: {bin_name}",
                command_preview=command_preview,
            )

        try:
            child = pexpect.spawn(
                bin_path,
                args,
                cwd=str(self.cwd),
                encoding="utf-8",
                timeout=timeout,
                codec_errors="replace",
            )
        except Exception as exc:  # noqa: BLE001
            return CliResult(
                elapsed_sec=time.perf_counter() - started_at,
                error_code="cli_spawn_failed",
                error=f"{type(exc).__name__}: {exc}",
                command_preview=command_preview,
            )

        output = ""
        auth_hit = False
        timed_out = False
        try:
            while True:
                try:
                    chunk = child.read_nonblocking(size=4096, timeout=2)
                    if chunk:
                        output += chunk
                        error_code, _error = self._classify_antigravity_failure(output)
                        if error_code == "auth_required":
                            auth_hit = True
                            break
                except pexpect.exceptions.TIMEOUT:
                    if time.perf_counter() - started_at > timeout:
                        timed_out = True
                        break
                    continue
                except pexpect.exceptions.EOF:
                    break
        finally:
            if child.isalive():
                try:
                    child.close(force=True)
                except Exception:  # noqa: BLE001
                    pass

        elapsed = time.perf_counter() - started_at
        text = ANSI_RE.sub("", output).strip()
        log_text = ""
        if log_path:
            try:
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                log_text = ""
        combined = f"{text}\n{log_text}".strip()
        exit_code = child.exitstatus

        error_code, error = self._classify_antigravity_failure(combined)
        if error_code == "unsupported_location" and text and exit_code == 0:
            error_code = None
            error = ""
        if error_code or auth_hit:
            return CliResult(
                text=text,
                raw={"stdout": text, "log_tail": log_text[-4000:]},
                elapsed_sec=elapsed,
                error_code=error_code or "auth_required",
                error=error or "agy requires OAuth authorization",
                exit_code=exit_code,
                command_preview=command_preview,
            )
        if timed_out:
            return CliResult(
                text=text,
                raw={"stdout": text, "log_tail": log_text[-4000:]},
                elapsed_sec=elapsed,
                error_code="timeout",
                error=f"CLI timed out after {timeout:.0f}s",
                exit_code=exit_code,
                command_preview=command_preview,
            )
        if not text:
            return CliResult(
                raw={"stdout": text, "log_tail": log_text[-4000:]},
                elapsed_sec=elapsed,
                error_code="empty_response",
                error="empty CLI response",
                exit_code=exit_code,
                command_preview=command_preview,
            )

        return CliResult(
            text=text,
            raw={"stdout": text, "log_tail": log_text[-4000:]},
            elapsed_sec=elapsed,
            exit_code=exit_code,
            command_preview=command_preview,
        )

    async def run_claude(
        self,
        prompt: str,
        *,
        model: str = "claude-sonnet",
        effort: str = "medium",
        timeout: float = 900,
    ) -> CliResult:
        # 安全策略 (2026-05-19 方案 C):
        # - --allowed-tools 只放 Read/Glob/Grep/Skill（只读 + skill 调用）
        #   禁了 Bash/Edit/Write/MultiEdit/NotebookEdit/Task/WebFetch 等危险工具
        # - --add-dir 加 data/skills 让 Claude Code 自动发现公司 skills
        #   ~/.claude/skills/brand-marketing 已软链到 DC-Agent/data/skills/brand-marketing
        # 效果: Claude 能用 brand-marketing 这种公司专家模板出策划方案，
        #       但不能跑 shell / 不能改文件
        # --max-turns 从 1 提到 3：给 Claude 一些"思考-读 skill-再写"的余地
        # 同时配合 NON_INTERACTIVE_GUARD prompt 约束，禁止它问澄清问题。
        claude_bin = os.environ.get(
            "DC_CLAUDE_CLI_BIN", self._default_claude_bin()
        ).strip()
        args = [
            claude_bin,
            "-p",
            prompt,
            "--model",
            model,
            "--effort",
            effort,
            "--output-format",
            "json",
            "--max-turns",
            "3",
            "--allowed-tools",
            "Read,Glob,Grep,Skill",
            "--add-dir",
            "/Users/dianchi/DC-Agent/data/skills",
        ]
        return await self._run_json_cli(
            args,
            timeout=timeout,
            text_field="result",
        )

    async def run_codex(
        self,
        prompt: str,
        *,
        model: str = "gpt-5.4",
        timeout: float = 300,
    ) -> CliResult:
        """Run Codex CLI in read-only one-shot mode and return the final message."""
        started_at = time.perf_counter()
        with tempfile.NamedTemporaryFile(
            mode="w+",
            suffix=".txt",
            encoding="utf-8",
            delete=True,
        ) as out:
            args = [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--sandbox",
                "read-only",
                "--model",
                model,
                "--output-last-message",
                out.name,
                "-",
            ]
            return await self._run_text_cli(
                args,
                prompt=prompt,
                timeout=timeout,
                output_file=Path(out.name),
                started_at=started_at,
            )

    async def run_grok_build(
        self,
        prompt: str,
        *,
        model: str = "grok-build",
        timeout: float = 60,
        web_search: bool = False,
    ) -> CliResult:
        """Run Grok Build through a clean headless one-shot command.

        Grok Build is fast in an already-warm terminal session, but cold-starting it
        inside the project directory can hang while it prepares a codebase session.
        This path keeps it outside the repo and disables agent features that are not
        needed for public-opinion answers.
        """
        args = [
            "grok",
            "-p",
            prompt,
            "--cwd",
            "/private/tmp",
            "--model",
            model,
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--no-memory",
            "--max-turns",
            "2",
            "--no-leader",
            "--no-plan",
            "--no-subagents",
            "--verbatim",
        ]
        if not web_search:
            args.append("--disable-web-search")
        return await self._run_grok_cli(args, timeout=timeout)

    def _classify_grok_failure(self, output: str) -> tuple[str | None, str]:
        lowered = output.lower()
        if any(
            marker in lowered
            for marker in (
                "rate limit",
                "ratelimit",
                "too many requests",
                "quota",
                "429",
            )
        ):
            return "rate_limited", "Grok Build rate limit or quota reached"
        if any(marker in lowered for marker in ("login", "oauth", "auth required")):
            return "auth_required", "Grok Build requires login"
        if "fs_permission_denied" in lowered or "permission denied" in lowered:
            return "permission_denied", "Grok Build permission denied"
        if "couldn't create session" in lowered:
            return "session_create_failed", "Grok Build could not create a session"
        return None, ""

    def _extract_grok_json_text(self, output: str) -> tuple[str, dict]:
        clean = ANSI_RE.sub("", output)
        last_payload: dict | None = None
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", clean):
            try:
                payload, _end = decoder.raw_decode(clean[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("text"):
                last_payload = payload
        if last_payload is not None:
            return str(last_payload.get("text") or "").strip(), last_payload

        lines: list[str] = []
        for line in clean.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if (
                " WARN" in stripped
                or " ERROR" in stripped
                or "repo_state.git.collect" in stripped
                or "Codebase upload failed" in stripped
                or "Caused by:" in stripped
            ):
                continue
            lines.append(stripped)
        return "\n".join(lines).strip(), {"stdout": clean}

    async def _run_grok_cli(self, args: list[str], *, timeout: float) -> CliResult:
        started_at = time.perf_counter()
        command_preview = shlex.join(args)
        bin_name = Path(args[0]).name if args else ""
        if bin_name not in self.ALLOWED_BINS:
            return CliResult(
                elapsed_sec=0.0,
                error_code="bin_not_allowed",
                error=f"CLI binary is not allowed: {bin_name}",
                command_preview=command_preview,
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.cwd),
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            return CliResult(
                elapsed_sec=time.perf_counter() - started_at,
                error_code="cli_not_found",
                error=str(exc),
                command_preview=command_preview,
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await self._kill_process_tree(proc)
            return CliResult(
                elapsed_sec=time.perf_counter() - started_at,
                error_code="timeout",
                error=f"CLI timed out after {timeout:.0f}s",
                command_preview=command_preview,
            )

        elapsed = time.perf_counter() - started_at
        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        combined = f"{stdout}\n{stderr}".strip()
        error_code, error = self._classify_grok_failure(combined)
        text, payload = self._extract_grok_json_text(combined)

        if proc.returncode != 0 or error_code:
            return CliResult(
                text=text,
                raw=payload | {"stderr": stderr},
                elapsed_sec=elapsed,
                error_code=error_code or "exit_code",
                error=error or stderr or stdout,
                exit_code=proc.returncode,
                command_preview=command_preview,
            )
        if not text:
            return CliResult(
                raw=payload | {"stderr": stderr},
                elapsed_sec=elapsed,
                error_code="empty_response",
                error=stderr or "empty CLI response",
                exit_code=proc.returncode,
                command_preview=command_preview,
            )
        return CliResult(
            text=text,
            raw=payload | {"stderr": stderr},
            elapsed_sec=elapsed,
            exit_code=proc.returncode,
            command_preview=command_preview,
        )

    async def _run_json_cli(
        self,
        args: list[str],
        *,
        timeout: float,
        text_field: str,
    ) -> CliResult:
        started_at = time.perf_counter()
        command_preview = shlex.join(args)
        bin_name = Path(args[0]).name if args else ""
        if bin_name not in self.ALLOWED_BINS:
            return CliResult(
                elapsed_sec=0.0,
                error_code="bin_not_allowed",
                error=f"CLI binary is not allowed: {bin_name}",
                command_preview=command_preview,
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.cwd),
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            return CliResult(
                elapsed_sec=time.perf_counter() - started_at,
                error_code="cli_not_found",
                error=str(exc),
                command_preview=command_preview,
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await self._kill_process_tree(proc)
            return CliResult(
                elapsed_sec=time.perf_counter() - started_at,
                error_code="timeout",
                error=f"CLI timed out after {timeout:.0f}s",
                command_preview=command_preview,
            )

        elapsed = time.perf_counter() - started_at
        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            return CliResult(
                elapsed_sec=elapsed,
                error_code="exit_code",
                error=stderr or stdout,
                exit_code=proc.returncode,
                command_preview=command_preview,
            )

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return CliResult(
                elapsed_sec=elapsed,
                error_code="json_parse",
                error=f"{exc}: {stdout[:500]}",
                exit_code=proc.returncode,
                command_preview=command_preview,
            )

        if payload.get("is_error"):
            return CliResult(
                raw=payload,
                elapsed_sec=elapsed,
                error_code="cli_error",
                error=str(payload.get(text_field) or payload),
                exit_code=proc.returncode,
                command_preview=command_preview,
            )

        text = str(payload.get(text_field) or "").strip()
        if not text:
            return CliResult(
                raw=payload,
                elapsed_sec=elapsed,
                error_code="empty_response",
                error=f"Missing JSON field: {text_field}",
                exit_code=proc.returncode,
                command_preview=command_preview,
            )

        model_usage = {}
        if isinstance(payload.get("modelUsage"), dict):
            model_usage = payload["modelUsage"]
        elif isinstance(payload.get("stats"), dict):
            model_usage = payload["stats"]
        return CliResult(
            text=text,
            raw=payload,
            model_usage=model_usage,
            elapsed_sec=elapsed,
            exit_code=proc.returncode,
            command_preview=command_preview,
        )

    async def _run_text_cli(
        self,
        args: list[str],
        *,
        prompt: str,
        timeout: float,
        output_file: Path,
        started_at: float | None = None,
    ) -> CliResult:
        started_at = started_at or time.perf_counter()
        command_preview = shlex.join(args)
        bin_name = Path(args[0]).name if args else ""
        if bin_name not in self.ALLOWED_BINS:
            return CliResult(
                elapsed_sec=0.0,
                error_code="bin_not_allowed",
                error=f"CLI binary is not allowed: {bin_name}",
                command_preview=command_preview,
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.cwd),
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            return CliResult(
                elapsed_sec=time.perf_counter() - started_at,
                error_code="cli_not_found",
                error=str(exc),
                command_preview=command_preview,
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await self._kill_process_tree(proc)
            return CliResult(
                elapsed_sec=time.perf_counter() - started_at,
                error_code="timeout",
                error=f"CLI timed out after {timeout:.0f}s",
                command_preview=command_preview,
            )

        elapsed = time.perf_counter() - started_at
        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            return CliResult(
                elapsed_sec=elapsed,
                error_code="exit_code",
                error=stderr or stdout,
                exit_code=proc.returncode,
                command_preview=command_preview,
            )

        try:
            text = output_file.read_text(encoding="utf-8").strip()
        except Exception as exc:  # noqa: BLE001
            text = ""
            stderr = f"{stderr}\noutput file read failed: {exc}".strip()

        if not text:
            text = stdout
        if not text:
            return CliResult(
                elapsed_sec=elapsed,
                error_code="empty_response",
                error=stderr or "empty CLI response",
                exit_code=proc.returncode,
                command_preview=command_preview,
            )

        return CliResult(
            text=text,
            raw={"stdout": stdout, "stderr": stderr},
            elapsed_sec=elapsed,
            exit_code=proc.returncode,
            command_preview=command_preview,
        )

    async def _run_stdout_cli(
        self,
        args: list[str],
        *,
        prompt: str,
        timeout: float,
        text_fields: tuple[str, ...],
    ) -> CliResult:
        started_at = time.perf_counter()
        command_preview = shlex.join(args)
        bin_name = Path(args[0]).name if args else ""
        if bin_name not in self.ALLOWED_BINS:
            return CliResult(
                elapsed_sec=0.0,
                error_code="bin_not_allowed",
                error=f"CLI binary is not allowed: {bin_name}",
                command_preview=command_preview,
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.cwd),
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            return CliResult(
                elapsed_sec=time.perf_counter() - started_at,
                error_code="cli_not_found",
                error=str(exc),
                command_preview=command_preview,
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await self._kill_process_tree(proc)
            return CliResult(
                elapsed_sec=time.perf_counter() - started_at,
                error_code="timeout",
                error=f"CLI timed out after {timeout:.0f}s",
                command_preview=command_preview,
            )

        elapsed = time.perf_counter() - started_at
        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            return CliResult(
                elapsed_sec=elapsed,
                error_code="exit_code",
                error=stderr or stdout,
                exit_code=proc.returncode,
                command_preview=command_preview,
            )

        raw: dict = {"stdout": stdout, "stderr": stderr}
        text = stdout
        if stdout.startswith("{"):
            try:
                payload = json.loads(stdout)
                raw = payload if isinstance(payload, dict) else raw
                if isinstance(payload, dict):
                    for field_name in text_fields:
                        value = payload.get(field_name)
                        if value:
                            text = str(value).strip()
                            break
            except json.JSONDecodeError:
                text = stdout

        if not text:
            return CliResult(
                raw=raw,
                elapsed_sec=elapsed,
                error_code="empty_response",
                error=stderr or "empty CLI response",
                exit_code=proc.returncode,
                command_preview=command_preview,
            )

        return CliResult(
            text=text,
            raw=raw,
            elapsed_sec=elapsed,
            exit_code=proc.returncode,
            command_preview=command_preview,
        )
