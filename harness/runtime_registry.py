"""Typed runtime status registry for Hermes deep-task surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    runtime: str
    implemented: bool
    enabled: bool
    reason: str
    adapter_wired: bool = False

    def to_dashboard_payload(self) -> dict[str, object]:
        return asdict(self)


class RuntimeRegistry:
    _DEFAULT_RUNTIMES = (
        "claude_cli",
        "codex_cli",
        "gemini_cli",
        "hermes_agent",
    )

    def __init__(self, statuses: Mapping[str, RuntimeStatus]) -> None:
        self._statuses = dict(statuses)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> RuntimeRegistry:
        statuses = {
            "claude_cli": RuntimeStatus(
                runtime="claude_cli",
                implemented=True,
                enabled=True,
                reason="available",
                adapter_wired=True,
            ),
            "codex_cli": RuntimeStatus(
                runtime="codex_cli",
                implemented=True,
                enabled=True,
                reason="available",
                adapter_wired=True,
            ),
            "gemini_cli": RuntimeStatus(
                runtime="gemini_cli",
                implemented=True,
                enabled=False,
                reason="adapter_not_wired",
                adapter_wired=False,
            ),
            "hermes_agent": RuntimeStatus(
                runtime="hermes_agent",
                implemented=True,
                enabled=False,
                reason="adapter_not_wired",
                adapter_wired=False,
            ),
        }
        if env.get("DC_DISABLE_HERMES_AGENT") == "1":
            statuses["hermes_agent"] = RuntimeStatus(
                runtime="hermes_agent",
                implemented=True,
                enabled=False,
                reason="disabled_by_env",
                adapter_wired=False,
            )
        return cls(statuses)

    def get(self, runtime: str) -> RuntimeStatus:
        return self._statuses.get(
            runtime,
            RuntimeStatus(
                runtime=runtime,
                implemented=False,
                enabled=False,
                reason="unknown_runtime",
            ),
        )

    def to_dashboard_payload(self) -> dict[str, object]:
        return {
            "runtimes": [
                self._statuses[runtime].to_dashboard_payload()
                for runtime in sorted(self._statuses)
            ],
        }
