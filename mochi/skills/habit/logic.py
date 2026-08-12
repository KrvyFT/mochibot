"""Habit logic — frequency parsing and time extraction.

Pure computation: frequency parsing, day filtering, time marker extraction.
No DB, no IO, no LLM calls.
"""

import re

_FREQ_RE = re.compile(r'^(daily|weekly):(\d+)$')
_FREQ_ON_RE = re.compile(r'^weekly_on:([a-z,]+):(\d+)$')
_DAY_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_frequency(freq: str) -> tuple[str, int] | None:
    """Parse frequency string into (cycle, target) or None.

    Supported formats:
      - "daily:N"  — N times per day
      - "weekly:N" — N times per week
      - "weekly_on:DAY,...:N" — N times per week, only on specified days
        (e.g. "weekly_on:sat,sun:1")
    """
    m = _FREQ_RE.match(freq)
    if m:
        return m.group(1), int(m.group(2))
    m = _FREQ_ON_RE.match(freq)
    if m:
        days_str, target = m.group(1), int(m.group(2))
        days = days_str.split(",")
        if all(d in _DAY_MAP for d in days) and days:
            return "weekly", int(target)
    return None


def get_allowed_days(freq: str) -> set[int] | None:
    """Extract allowed weekday numbers from weekly_on frequency (0=Mon..6=Sun).

    Returns None for daily or plain weekly (all days allowed).
    """
    m = _FREQ_ON_RE.match(freq)
    if not m:
        return None
    days = m.group(1).split(",")
    return {_DAY_MAP[d] for d in days if d in _DAY_MAP}
