"""Boundary object for Hermes deep-task runtime integration."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.callbacks import TaskCallbackPayload, TaskCallbackSink
from harness.quota_gate import QuotaGate
from harness.resources import DEFAULT_RESOURCE_CONFIGS, ResourceConfig, ResourceKey

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HermesTaskRequest:
    router_decision: dict[str, Any]
    user_input: str
    queue_job_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    target_runtime: str | None = None


@dataclass(frozen=True, slots=True)
class ClaudeCliResult:
    text: str
    raw_events: list[dict[str, Any]]
    stderr: str
    returncode: int | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())


@dataclass(frozen=True, slots=True)
class CodexCliResult:
    """Result from Codex CLI (dpr protocol / Responses or app-server stream)."""

    text: str
    raw_events: list[dict[str, Any]]
    stderr: str
    returncode: int | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())


class _NullCallbackSink:
    async def send(self, payload: TaskCallbackPayload) -> None:
        """Drop callback payloads when no external sink is configured."""


class HermesBridge:
    def __init__(
        self,
        *,
        quota_gate: QuotaGate | None = None,
        callback_sink: TaskCallbackSink | None = None,
        resource_configs: dict[str, ResourceConfig] | None = None,
        cwd: str | Path | None = None,
    ) -> None:
        self.quota_gate = quota_gate
        self.callback_sink = callback_sink or _NullCallbackSink()
        self.resource_configs = resource_configs or DEFAULT_RESOURCE_CONFIGS
        self.cwd = Path(cwd) if cwd is not None else None
        self._tasks: set[asyncio.Task[None]] = set()

    async def submit(self, request: HermesTaskRequest) -> str:
        runtime = self._resolve_target_runtime(request)
        if runtime not in ("claude_cli", "codex_cli"):
            msg = (
                f"HermesBridge runtime {runtime!r} is not implemented yet. "
                "Implemented runtimes: claude_cli, codex_cli (dpr protocol). "
                "TODO runtimes: claude_oauth, anthropic_api, gemini_cli, hermes_agent."
            )
            raise NotImplementedError(msg)

        task = asyncio.create_task(
            self._run_request(request),
            name=f"hermes-bridge-{request.queue_job_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return request.queue_job_id

    async def drain(self) -> None:
        """Wait for currently submitted background tasks to settle."""
        if not self._tasks:
            return
        await asyncio.gather(*tuple(self._tasks))

    async def _run_request(self, request: HermesTaskRequest) -> None:
        runtime = self._resolve_target_runtime(request)
        try:
            if runtime == "codex_cli":
                result = await self._run_codex_cli(request)
                ok = result.ok
                text = result.text
                raw = result.raw_events
                err = (
                    result.error
                    or result.stderr
                    or "Codex CLI (dpr) failed without an error"
                )
            else:
                result = await self._run_claude_cli(request)
                ok = result.ok
                text = result.text
                raw = result.raw_events
                err = (
                    result.error
                    or result.stderr
                    or "Claude CLI failed without an error"
                )
        except asyncio.CancelledError:
            await self._finish_failed(request, f"{runtime} task cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("HermesBridge request failed: %s", request.queue_job_id)
            await self._finish_failed(request, str(exc))
            return

        if ok:
            payload = {
                "text": text,
                "raw": raw,
                "runtime": runtime,
            }
            await self._finish_completed(request, payload)
            return

        await self._finish_failed(request, err)

    async def _run_claude_cli(self, request: HermesTaskRequest) -> ClaudeCliResult:
        prompt = self._prompt_for_request(request)
        args = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        stdin_text = self._stdin_jsonl_for_request(request)
        if stdin_text:
            args.extend(["--input-format", "stream-json"])

        env = self._subprocess_env()
        started = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin_text else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(self.cwd) if self.cwd is not None else None,
        )

        if stdin_text and started.stdin is not None:
            started.stdin.write(stdin_text.encode("utf-8"))
            await started.stdin.drain()
            started.stdin.close()

        timeout_seconds = self._timeout_seconds_for_request(request)
        raw_events: list[dict[str, Any]] = []
        stdout_task = asyncio.create_task(
            self._read_stream_json(started.stdout, raw_events)
        )
        stderr_task = asyncio.create_task(self._read_text_stream(started.stderr))

        try:
            returncode = await asyncio.wait_for(started.wait(), timeout_seconds)
        except TimeoutError:
            await self._terminate_process(started)
            await self._cancel_task(stdout_task)
            stderr = await self._finish_text_task(stderr_task)
            return ClaudeCliResult(
                text="",
                raw_events=raw_events,
                stderr=stderr,
                returncode=started.returncode,
                error=f"Claude CLI timeout after {timeout_seconds:.0f}s",
            )

        await stdout_task
        stderr = await self._finish_text_task(stderr_task)

        stream_error = self._stream_error(raw_events)
        if returncode != 0:
            error = stderr or stream_error or f"Claude CLI exited with {returncode}"
            return ClaudeCliResult(
                text="",
                raw_events=raw_events,
                stderr=stderr,
                returncode=returncode,
                error=error,
            )

        if stream_error:
            return ClaudeCliResult(
                text="",
                raw_events=raw_events,
                stderr=stderr,
                returncode=returncode,
                error=stream_error,
            )

        result_text = self._result_text(raw_events)
        if not result_text:
            return ClaudeCliResult(
                text="",
                raw_events=raw_events,
                stderr=stderr,
                returncode=returncode,
                error="Claude CLI stream did not include a result event",
            )

        return ClaudeCliResult(
            text=result_text,
            raw_events=raw_events,
            stderr=stderr,
            returncode=returncode,
        )

    async def _run_codex_cli(self, request: HermesTaskRequest) -> CodexCliResult:
        """Run deep task via Codex CLI using its dpr protocol (stream-json or app-server events).

        Mirrors _run_claude_cli but invokes the `codex` binary (from Codex.app or PATH).
        The exact wire format for Codex "dpr" (Responses or internal event stream) is
        parsed with the same line-delimited JSON reader; result extraction falls back
        to common keys. Extend _extract_text / event handling as needed for full dpr shapes.
        """
        prompt = self._prompt_for_request(request)
        # Codex CLI often supports similar -p + output flags; adjust if dpr uses
        # different (e.g. codex exec or specific --format dpr / responses).
        args = [
            "codex",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        stdin_text = self._stdin_jsonl_for_request(request)
        if stdin_text:
            args.extend(["--input-format", "stream-json"])

        env = self._subprocess_env_for_codex()
        started = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin_text else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(self.cwd) if self.cwd is not None else None,
        )

        if stdin_text and started.stdin is not None:
            started.stdin.write(stdin_text.encode("utf-8"))
            await started.stdin.drain()
            started.stdin.close()

        timeout_seconds = self._timeout_seconds_for_request(request)
        raw_events: list[dict[str, Any]] = []
        stdout_task = asyncio.create_task(
            self._read_stream_json(started.stdout, raw_events)
        )
        stderr_task = asyncio.create_task(self._read_text_stream(started.stderr))

        try:
            returncode = await asyncio.wait_for(started.wait(), timeout_seconds)
        except TimeoutError:
            await self._terminate_process(started)
            await self._cancel_task(stdout_task)
            stderr = await self._finish_text_task(stderr_task)
            return CodexCliResult(
                text="",
                raw_events=raw_events,
                stderr=stderr,
                returncode=started.returncode,
                error=f"Codex CLI (dpr) timeout after {timeout_seconds:.0f}s",
            )

        await stdout_task
        stderr = await self._finish_text_task(stderr_task)

        stream_error = self._stream_error(raw_events)
        if returncode != 0:
            error = (
                stderr or stream_error or f"Codex CLI (dpr) exited with {returncode}"
            )
            return CodexCliResult(
                text="",
                raw_events=raw_events,
                stderr=stderr,
                returncode=returncode,
                error=error,
            )

        if stream_error:
            return CodexCliResult(
                text="",
                raw_events=raw_events,
                stderr=stderr,
                returncode=returncode,
                error=stream_error,
            )

        result_text = self._result_text(raw_events)
        if not result_text:
            return CodexCliResult(
                text="",
                raw_events=raw_events,
                stderr=stderr,
                returncode=returncode,
                error="Codex CLI (dpr) stream did not include a result event",
            )

        return CodexCliResult(
            text=result_text,
            raw_events=raw_events,
            stderr=stderr,
            returncode=returncode,
        )

    async def _finish_completed(
        self,
        request: HermesTaskRequest,
        result: dict[str, Any],
    ) -> None:
        if self.quota_gate is not None:
            await self.quota_gate.complete(request.queue_job_id, result=result)
        await self.callback_sink.send(
            TaskCallbackPayload(
                job_id=request.queue_job_id,
                session_id=request.payload.get("session_id"),
                status="completed",
                result=result,
            )
        )

    async def _finish_failed(self, request: HermesTaskRequest, error: str) -> None:
        if self.quota_gate is not None:
            await self.quota_gate.fail(request.queue_job_id, error)
        await self.callback_sink.send(
            TaskCallbackPayload(
                job_id=request.queue_job_id,
                session_id=request.payload.get("session_id"),
                status="failed",
                error=error,
            )
        )

    async def _read_stream_json(
        self,
        stream: asyncio.StreamReader | None,
        raw_events: list[dict[str, Any]],
    ) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError as exc:
                raw_events.append(
                    {
                        "type": "parse_error",
                        "error": str(exc),
                        "raw": text,
                    }
                )
                continue
            if isinstance(event, dict):
                raw_events.append(event)
            else:
                raw_events.append({"type": "non_object_event", "raw": event})

    async def _read_text_stream(self, stream: asyncio.StreamReader | None) -> str:
        if stream is None:
            return ""
        chunks: list[str] = []
        while True:
            line = await stream.readline()
            if not line:
                return "".join(chunks).strip()
            chunks.append(line.decode("utf-8", errors="replace"))

    async def _finish_text_task(self, task: asyncio.Task[str]) -> str:
        try:
            return await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return str(exc)

    async def _cancel_task(self, task: asyncio.Task[Any]) -> None:
        if task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return

    async def _terminate_process(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()
            await proc.wait()

    def _resolve_target_runtime(self, request: HermesTaskRequest) -> str:
        explicit = request.target_runtime or request.payload.get("target_runtime")
        if explicit:
            return str(explicit)

        decision = request.router_decision
        decision_runtime = decision.get("target_runtime")
        if decision_runtime:
            return str(decision_runtime)

        provider_id = str(decision.get("provider_id") or "")
        backend = str(decision.get("backend") or "")
        if provider_id.startswith("cli/claude") or backend == "claude":
            return "claude_cli"
        if provider_id.startswith("cli/codex") or backend == "codex":
            return (
                "codex_cli"  # uses dpr protocol (codex app-server / responses stream)
            )
        if provider_id.startswith("cli/gemini") or backend == "gemini":
            return "gemini_cli"
        return "unknown"

    def _prompt_for_request(self, request: HermesTaskRequest) -> str:
        payload = request.payload
        prompt = (
            payload.get("prompt") or payload.get("message_text") or request.user_input
        )
        return str(prompt or "")

    def _stdin_jsonl_for_request(self, request: HermesTaskRequest) -> str:
        stdin_jsonl = request.payload.get("stdin_jsonl")
        if isinstance(stdin_jsonl, str) and stdin_jsonl.strip():
            return stdin_jsonl.rstrip("\n") + "\n"

        stream_input = request.payload.get("stream_json_input")
        if isinstance(stream_input, dict):
            return json.dumps(stream_input, ensure_ascii=False) + "\n"
        if isinstance(stream_input, list):
            return "".join(
                json.dumps(item, ensure_ascii=False) + "\n" for item in stream_input
            )

        messages = request.payload.get("messages")
        if isinstance(messages, list) and messages:
            return "".join(
                json.dumps(message, ensure_ascii=False) + "\n" for message in messages
            )
        return ""

    def _subprocess_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        return env

    def _subprocess_env_for_codex(self) -> dict[str, str]:
        """Env for Codex CLI (dpr protocol). Avoid leaking keys that would bypass Codex OAuth/CLI billing."""
        env = dict(os.environ)
        # Drop keys that would force non-Codex paths.
        for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
            env.pop(k, None)
        # Codex specific disables if supported by the binary.
        env.setdefault("CODEX_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        return env

    def _timeout_seconds_for_request(self, request: HermesTaskRequest) -> float:
        resource_key = self._primary_resource_key(request)
        config = self.resource_configs.get(
            resource_key, ResourceConfig(key=resource_key)
        )
        return float(config.estimated_run_seconds * 3)

    def _primary_resource_key(self, request: HermesTaskRequest) -> str:
        payload = request.payload
        resource_key = payload.get("primary_resource_key") or payload.get(
            "resource_key"
        )
        if resource_key:
            return str(resource_key)

        resource_keys = payload.get("resource_keys") or request.router_decision.get(
            "resource_keys"
        )
        if isinstance(resource_keys, (list, tuple)) and resource_keys:
            return str(resource_keys[0])

        runtime = self._resolve_target_runtime(request)
        if runtime == "codex_cli":
            # Prefer high tier for deep codex tasks if present, else global.
            if ResourceKey.CODEX_CLI_XHIGH.value in self.resource_configs:
                return ResourceKey.CODEX_CLI_XHIGH.value
            if ResourceKey.CODEX_CLI_HIGH.value in self.resource_configs:
                return ResourceKey.CODEX_CLI_HIGH.value
            return ResourceKey.CODEX_CLI_GLOBAL.value

        return ResourceKey.CLAUDE_CLI_GLOBAL.value

    def _stream_error(self, raw_events: list[dict[str, Any]]) -> str | None:
        for event in raw_events:
            subtype = str(event.get("subtype") or "")
            if subtype.startswith("error_"):
                return str(
                    event.get("error")
                    or event.get("message")
                    or event.get("result")
                    or event
                )
            if event.get("type") == "error" or event.get("is_error"):
                return str(event.get("error") or event.get("message") or event)
        return None

    def _result_text(self, raw_events: list[dict[str, Any]]) -> str:
        for event in reversed(raw_events):
            if event.get("type") != "result":
                continue
            text = self._extract_text(event)
            if text:
                return text
        return ""

    def _extract_text(self, event: dict[str, Any]) -> str:
        for key in ("result", "text", "response", "message", "content"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list):
                parts = [
                    str(part.get("text"))
                    for part in value
                    if isinstance(part, dict) and part.get("text")
                ]
                if parts:
                    return "\n".join(parts).strip()
            if isinstance(value, dict):
                text = self._extract_text(value)
                if text:
                    return text
        return ""
