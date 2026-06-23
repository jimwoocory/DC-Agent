from __future__ import annotations

import re
import time
from collections import Counter
from threading import Lock
from typing import Final

_METRIC_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9_:]")


class DCMetricsRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: Counter[str] = Counter()
        self._gauges: dict[str, float] = {}
        self._started_at = time.time()

    def increment(self, name: str, amount: int = 1) -> int:
        metric_name = normalize_metric_name(name)
        with self._lock:
            self._counters[metric_name] += int(amount)
            return self._counters[metric_name]

    def set_gauge(self, name: str, value: float) -> float:
        metric_name = normalize_metric_name(name)
        with self._lock:
            self._gauges[metric_name] = float(value)
            return self._gauges[metric_name]

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "started_at": self._started_at,
                "uptime_sec": max(0.0, time.time() - self._started_at),
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
            }

    def render_prometheus_text(self) -> str:
        snapshot = self.snapshot()
        lines = [
            "# HELP dc_agent_uptime_seconds Process uptime in seconds.",
            "# TYPE dc_agent_uptime_seconds gauge",
            f"dc_agent_uptime_seconds {snapshot['uptime_sec']:.3f}",
        ]
        counters = snapshot["counters"]
        if isinstance(counters, dict):
            for name, value in sorted(counters.items()):
                lines.append(f"# TYPE {name} counter")
                lines.append(f"{name} {int(value)}")
        gauges = snapshot["gauges"]
        if isinstance(gauges, dict):
            for name, value in sorted(gauges.items()):
                lines.append(f"# TYPE {name} gauge")
                lines.append(f"{name} {float(value):.3f}")
        return "\n".join(lines) + "\n"


def normalize_metric_name(name: str) -> str:
    normalized = _METRIC_NAME_RE.sub("_", name.strip())
    normalized = normalized.strip("_")
    if not normalized:
        return "dc_agent_metric"
    if normalized[0].isdigit():
        return f"dc_agent_{normalized}"
    return normalized


registry = DCMetricsRegistry()


def increment_counter(name: str, amount: int = 1) -> int:
    return registry.increment(name, amount)


def set_gauge(name: str, value: float) -> float:
    return registry.set_gauge(name, value)


def snapshot() -> dict[str, object]:
    return registry.snapshot()
