"""Grok Build worker facade for the public-opinion route.

This is intentionally a worker boundary, not a direct router adapter. The current
backend uses a clean one-shot Grok Build command, but callers only talk to the
single-concurrency worker so we can later swap in a true stdio/PTY resident
session without changing router code.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cli_runner import CliResult, CliRunner

_STATE_PATH = Path(
    os.environ.get(
        "DC_GROK_WORKER_STATE_PATH",
        "/Users/dianchi/DC-Agent/data/grok_worker_state.json",
    )
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class GrokWorkerConfig:
    model: str = os.environ.get("DC_GROK_BUILD_MODEL", "grok-build")
    timeout_seconds: float = _env_float("DC_GROK_BUILD_TIMEOUT_SECONDS", 60.0)
    cooldown_seconds: float = _env_float("DC_GROK_BUILD_COOLDOWN_SECONDS", 120.0)
    failure_cooldown_seconds: float = _env_float(
        "DC_GROK_BUILD_FAILURE_COOLDOWN_SECONDS", 10 * 60.0
    )
    window_seconds: int = _env_int("DC_GROK_BUILD_WINDOW_SECONDS", 2 * 60 * 60)
    max_calls_per_window: int = _env_int("DC_GROK_BUILD_MAX_CALLS_PER_WINDOW", 40)
    max_cooldown_wait_seconds: float = _env_float(
        "DC_GROK_BUILD_MAX_COOLDOWN_WAIT_SECONDS", 0.0
    )
    web_search: bool = os.environ.get("DC_GROK_BUILD_WEB_SEARCH", "0") == "1"


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(_STATE_PATH.suffix + ".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(_STATE_PATH)
    except Exception:
        pass


class GrokBuildWorker:
    """Single-concurrency Grok Build worker with local quota protection."""

    def __init__(self, config: GrokWorkerConfig | None = None) -> None:
        self.config = config or GrokWorkerConfig()
        self._lock = asyncio.Lock()

    def get_state(self) -> dict[str, Any]:
        state = _load_state()
        now = time.time()
        disabled_until = float(state.get("disabled_until") or 0)
        state["available"] = disabled_until <= now
        state["remaining_seconds"] = max(0, int(disabled_until - now))
        state["calls_in_window"] = len(self._recent_calls(state, now=now, mutate=False))
        return state

    async def ask_public_opinion(self, prompt: str) -> CliResult:
        """Run one public-opinion prompt through the protected Grok channel."""
        async with self._lock:
            state = _load_state()
            now = time.time()
            disabled_until = float(state.get("disabled_until") or 0)
            if disabled_until > now:
                return CliResult(
                    error_code="grok_circuit_open",
                    error=f"Grok Build is cooling down for {int(disabled_until - now)}s",
                )

            calls = self._recent_calls(state, now=now, mutate=True)
            if len(calls) >= self.config.max_calls_per_window:
                return CliResult(
                    error_code="grok_rate_window",
                    error=(
                        "Local Grok Build window limit reached: "
                        f"{len(calls)}/{self.config.max_calls_per_window}"
                    ),
                )

            wait_seconds = self._cooldown_wait_seconds(state, now)
            if wait_seconds > self.config.max_cooldown_wait_seconds:
                return CliResult(
                    error_code="grok_local_cooldown",
                    error=f"Grok Build cooldown remaining: {int(wait_seconds)}s",
                )
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)

            started_at = time.time()
            result = await CliRunner(cwd=Path("/private/tmp")).run_grok_build(
                prompt,
                model=self.config.model,
                timeout=self.config.timeout_seconds,
                web_search=self.config.web_search,
            )
            if result.ok:
                calls.append(started_at)
                state.update(
                    {
                        "status": "healthy",
                        "disabled_until": 0,
                        "consecutive_failures": 0,
                        "last_success_at": time.time(),
                        "last_success_elapsed_sec": round(result.elapsed_sec, 3),
                        "last_error_code": "",
                        "last_error": "",
                        "calls": calls,
                    }
                )
            else:
                failures = int(state.get("consecutive_failures") or 0) + 1
                failure_cooldown = self._failure_cooldown(result.error_code, failures)
                state.update(
                    {
                        "status": "open" if failure_cooldown else "degraded",
                        "disabled_until": time.time() + failure_cooldown,
                        "consecutive_failures": failures,
                        "last_failure_at": time.time(),
                        "last_error_code": result.error_code or "unknown",
                        "last_error": (result.error or "")[:500],
                        "calls": calls,
                    }
                )
            _save_state(state)
            return result

    def _recent_calls(
        self,
        state: dict[str, Any],
        *,
        now: float,
        mutate: bool,
    ) -> list[float]:
        cutoff = now - self.config.window_seconds
        raw_calls = state.get("calls") or []
        calls = [
            float(item)
            for item in raw_calls
            if isinstance(item, (int, float)) and float(item) >= cutoff
        ]
        if mutate:
            state["calls"] = calls
        return calls

    def _cooldown_wait_seconds(self, state: dict[str, Any], now: float) -> float:
        last_success = float(state.get("last_success_at") or 0)
        if not last_success:
            return 0.0
        return max(0.0, last_success + self.config.cooldown_seconds - now)

    def _failure_cooldown(self, error_code: str | None, failures: int) -> float:
        if error_code in {"auth_required", "rate_limited", "permission_denied"}:
            return self.config.failure_cooldown_seconds
        if error_code == "timeout" and failures >= 1:
            return min(self.config.failure_cooldown_seconds, 5 * 60.0)
        if failures >= 2:
            return min(self.config.failure_cooldown_seconds, 5 * 60.0)
        return 0.0


_WORKER: GrokBuildWorker | None = None


def get_grok_worker() -> GrokBuildWorker:
    global _WORKER
    if _WORKER is None:
        _WORKER = GrokBuildWorker()
    return _WORKER
