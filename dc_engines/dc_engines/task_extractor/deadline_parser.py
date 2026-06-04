"""中英文相对时间 → datetime 解析。

支持：
  - "今天" / "明天" / "后天" / "today" / "tomorrow"
  - "周N" / "下周N" / "周三前"
  - "N 天后" / "N 天内"
  - 显式日期 "5月20日" / "5/20" / "2026-05-20"
  - 含时分 "明天 9:00" / "今天 14:30"

无法解析返回 None（绝不抛异常，因为来自 LLM 不可控）。
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta

_WEEKDAY_ZH = {
    "周一": 0,
    "周二": 1,
    "周三": 2,
    "周四": 3,
    "周五": 4,
    "周六": 5,
    "周日": 6,
    "周天": 6,
    "星期一": 0,
    "星期二": 1,
    "星期三": 2,
    "星期四": 3,
    "星期五": 4,
    "星期六": 5,
    "星期日": 6,
    "礼拜一": 0,
    "礼拜二": 1,
    "礼拜三": 2,
    "礼拜四": 3,
    "礼拜五": 4,
    "礼拜六": 5,
    "礼拜日": 6,
}

_WEEKDAY_EN = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

_TIME_RE = re.compile(r"(\d{1,2})[:：](\d{2})")
_N_DAYS_RE = re.compile(r"(\d+)\s*天(?:之?后|内|之?内)?")
_N_HOURS_RE = re.compile(r"(\d+)\s*(?:个)?小时(?:之?后|内)?")
_DATE_SLASH_RE = re.compile(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?")
_DATE_ZH_RE = re.compile(r"(\d{1,2})月(\d{1,2})日?")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")


def _default_time() -> time:
    """缺省时间：当天 18:00（下班前）。"""
    return time(hour=18, minute=0)


def _combine(date_d, t: time | None = None) -> datetime:
    return datetime.combine(date_d, t or _default_time())


def parse_deadline(text: str | None, *, now: datetime) -> datetime | None:
    """从原始时间表达解析 deadline；解析失败返回 None，不抛错。

    ``now`` 由调用方注入以保证可测试性。
    """
    if not text or not text.strip():
        return None
    raw = text.strip()
    low = raw.lower()

    # 显式 ISO 日期：2026-05-20
    m = _ISO_DATE_RE.search(raw)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            base = datetime(year=y, month=mo, day=d)
            t = _parse_time(raw) or _default_time()
            return base.replace(hour=t.hour, minute=t.minute)
        except ValueError:
            pass

    # 中文 "5月20日"
    m = _DATE_ZH_RE.search(raw)
    if m:
        try:
            mo, d = int(m.group(1)), int(m.group(2))
            year = now.year
            # 已过去 → 明年
            cand = datetime(year=year, month=mo, day=d)
            if cand.date() < now.date():
                cand = cand.replace(year=year + 1)
            t = _parse_time(raw) or _default_time()
            return cand.replace(hour=t.hour, minute=t.minute)
        except ValueError:
            pass

    # 5/20 或 5-20
    m = _DATE_SLASH_RE.search(raw)
    if m and not _ISO_DATE_RE.search(raw):
        try:
            a, b = int(m.group(1)), int(m.group(2))
            year_part = m.group(3)
            if year_part:
                # 5/20/2026 或 5-20-26
                year = int(year_part)
                if year < 100:
                    year += 2000
                cand = datetime(year=year, month=a, day=b)
            else:
                year = now.year
                cand = datetime(year=year, month=a, day=b)
                if cand.date() < now.date():
                    cand = cand.replace(year=year + 1)
            t = _parse_time(raw) or _default_time()
            return cand.replace(hour=t.hour, minute=t.minute)
        except ValueError:
            pass

    # "今天 / 明天 / 后天" + 可选时间
    if "今天" in raw or "today" in low:
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        t = _parse_time(raw) or _default_time()
        return base.replace(hour=t.hour, minute=t.minute)
    if "明天" in raw or "tomorrow" in low:
        base = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        t = _parse_time(raw) or _default_time()
        return base.replace(hour=t.hour, minute=t.minute)
    if "后天" in raw:
        base = (now + timedelta(days=2)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        t = _parse_time(raw) or _default_time()
        return base.replace(hour=t.hour, minute=t.minute)

    # 周N / 下周N / 周三前
    next_week = "下周" in raw or "next week" in low
    for kw, wd in _WEEKDAY_ZH.items():
        if kw in raw:
            return _next_weekday(now, wd, advance_week=next_week)
    for kw, wd in _WEEKDAY_EN.items():
        if re.search(rf"\b{kw}\b", low):
            return _next_weekday(now, wd, advance_week=next_week)

    # N 天后/N 天内
    m = _N_DAYS_RE.search(raw)
    if m:
        days = int(m.group(1))
        if 0 < days <= 365:
            return _combine((now + timedelta(days=days)).date())

    # N 小时后
    m = _N_HOURS_RE.search(raw)
    if m:
        hours = int(m.group(1))
        if 0 < hours <= 168:
            return now + timedelta(hours=hours)

    # 显式 "急 / 紧急" 默认为今天结束
    if "紧急" in raw or "急" in raw or "asap" in low:
        return now.replace(hour=18, minute=0, second=0, microsecond=0)

    return None


def _parse_time(raw: str) -> time | None:
    m = _TIME_RE.search(raw)
    if m:
        hh = max(0, min(23, int(m.group(1))))
        mm = max(0, min(59, int(m.group(2))))
        return time(hour=hh, minute=mm)
    return None


def _next_weekday(
    now: datetime, target_wd: int, *, advance_week: bool = False
) -> datetime:
    """找最近的目标 weekday；同一天则按当天 default time。"""
    diff = (target_wd - now.weekday()) % 7
    if diff == 0:
        diff = 0 if not advance_week else 7
    if advance_week:
        diff += 7
    base = (now + timedelta(days=diff)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return base.replace(hour=18, minute=0)
