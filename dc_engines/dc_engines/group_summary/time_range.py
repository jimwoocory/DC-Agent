"""Parse Chinese / English time-range hints into a concrete TimeRange.

Examples handled:

- ``""`` / ``None``           → default: last 12h or 50 msgs, whichever yields fewer.
- ``"今天"`` / ``"today"``     → since 00:00 local today.
- ``"最近 50 条"`` / ``"last 50"`` → count-capped; window stays at default 12h.
- ``"从昨天 9:00 开始"``      → explicit start at yesterday 09:00 local.
- ``"最近一周"`` / ``"last week"`` → 7d window, default count cap.

Anything we cannot parse falls back to the default window — never raise on
user input, since the trigger comes from a group chat.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

DEFAULT_MAX_COUNT = 50
DEFAULT_WINDOW_HOURS = 12


@dataclass(frozen=True, slots=True)
class TimeRange:
    """Resolved time window for one summary request.

    The fetcher returns up to ``max_count`` messages whose timestamp falls in
    ``[start, end]`` (both inclusive). ``description`` is the human-readable
    label rendered into the markdown header.
    """

    start: datetime
    end: datetime
    max_count: int
    description: str


# ── 解析模式 ──────────────────────────────────────────────────────────────────

_COUNT_PATTERNS = (
    re.compile(r"最近\s*(\d+)\s*条"),
    re.compile(r"近\s*(\d+)\s*条"),
    re.compile(r"前\s*(\d+)\s*条"),
    re.compile(r"last\s+(\d+)", re.IGNORECASE),
)

_DAYS_PATTERNS = (
    (re.compile(r"最近\s*(\d+)\s*天"), 1),
    (re.compile(r"近\s*(\d+)\s*天"), 1),
    (re.compile(r"last\s+(\d+)\s*days?", re.IGNORECASE), 1),
)

_HOURS_PATTERNS = (
    (re.compile(r"最近\s*(\d+)\s*小时"), 1),
    (re.compile(r"近\s*(\d+)\s*小时"), 1),
    (re.compile(r"last\s+(\d+)\s*hours?", re.IGNORECASE), 1),
)

_EXPLICIT_TIME = re.compile(
    r"(?:从)?\s*(今天|昨天|前天|today|yesterday)\s*(\d{1,2})[:：](\d{2})",
    re.IGNORECASE,
)


def _start_of_day(now: datetime, *, days_ago: int = 0) -> datetime:
    base = now - timedelta(days=days_ago)
    return base.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_time_range(hint: str | None, *, now: datetime) -> TimeRange:
    """Return a TimeRange resolved against ``now`` (caller-provided clock).

    ``now`` is injected to keep this function deterministic in tests.
    Unknown hints fall back to the default window — never raises.
    """
    default_end = now
    default_start = now - timedelta(hours=DEFAULT_WINDOW_HOURS)
    default_desc = f"最近 {DEFAULT_MAX_COUNT} 条 / {DEFAULT_WINDOW_HOURS}h 内"

    if not hint or not hint.strip():
        return TimeRange(
            start=default_start,
            end=default_end,
            max_count=DEFAULT_MAX_COUNT,
            description=default_desc,
        )

    h = hint.strip().lower()

    # 显式时间 "今天 9:00" / "昨天 14:30" / "today 9:00"
    match = _EXPLICIT_TIME.search(h)
    if match:
        keyword, hh, mm = match.group(1), int(match.group(2)), int(match.group(3))
        # 边界保护
        hh = max(0, min(23, hh))
        mm = max(0, min(59, mm))
        days_ago = {
            "今天": 0,
            "today": 0,
            "昨天": 1,
            "yesterday": 1,
            "前天": 2,
        }.get(keyword, 0)
        start = (now - timedelta(days=days_ago)).replace(
            hour=hh, minute=mm, second=0, microsecond=0
        )
        return TimeRange(
            start=start,
            end=now,
            max_count=DEFAULT_MAX_COUNT,
            description=f"从 {start.strftime('%Y-%m-%d %H:%M')} 起",
        )

    # 整天关键词
    if "今天" in h or "today" in h:
        start = _start_of_day(now)
        return TimeRange(
            start=start,
            end=now,
            max_count=DEFAULT_MAX_COUNT,
            description="今天",
        )
    if "昨天" in h or "yesterday" in h:
        start = _start_of_day(now, days_ago=1)
        end = _start_of_day(now)  # 昨天结束 = 今天 00:00
        return TimeRange(
            start=start,
            end=end,
            max_count=DEFAULT_MAX_COUNT,
            description="昨天",
        )
    if "前天" in h:
        start = _start_of_day(now, days_ago=2)
        end = _start_of_day(now, days_ago=1)
        return TimeRange(
            start=start,
            end=end,
            max_count=DEFAULT_MAX_COUNT,
            description="前天",
        )

    # 周 / 月 关键词
    if "最近一周" in h or "最近 1 周" in h or "近一周" in h or "last week" in h:
        start = now - timedelta(days=7)
        return TimeRange(
            start=start,
            end=now,
            max_count=DEFAULT_MAX_COUNT * 4,
            description="最近一周",
        )
    if "本周" in h or "this week" in h:
        # 周一作为一周开始
        days_since_monday = now.weekday()
        start = _start_of_day(now, days_ago=days_since_monday)
        return TimeRange(
            start=start,
            end=now,
            max_count=DEFAULT_MAX_COUNT * 4,
            description="本周",
        )

    # 显式 N 条
    for pat in _COUNT_PATTERNS:
        m = pat.search(h)
        if m:
            count = max(1, min(int(m.group(1)), 500))
            return TimeRange(
                start=default_start,
                end=default_end,
                max_count=count,
                description=f"最近 {count} 条",
            )

    # 显式 N 天
    for pat, _ in _DAYS_PATTERNS:
        m = pat.search(h)
        if m:
            days = max(1, min(int(m.group(1)), 30))
            start = now - timedelta(days=days)
            return TimeRange(
                start=start,
                end=now,
                max_count=DEFAULT_MAX_COUNT * days,
                description=f"最近 {days} 天",
            )

    # 显式 N 小时
    for pat, _ in _HOURS_PATTERNS:
        m = pat.search(h)
        if m:
            hours = max(1, min(int(m.group(1)), 168))
            start = now - timedelta(hours=hours)
            return TimeRange(
                start=start,
                end=now,
                max_count=DEFAULT_MAX_COUNT,
                description=f"最近 {hours} 小时",
            )

    # 任何无法识别的 hint → 走默认
    return TimeRange(
        start=default_start,
        end=default_end,
        max_count=DEFAULT_MAX_COUNT,
        description=default_desc,
    )
