"""Tool policy — lightweight governance layer for tool execution.

Enforces:
  - Denylist: tools in TOOL_DENY_NAMES are always blocked
  - Rate limit: simple per-tool counter (resets on restart, per-minute window)

Denials are logged to standard Python logger (no separate JSONL files).
"""

import logging
import time
from dataclasses import dataclass
from threading import Lock

from mochi.config import TOOL_DENY_NAMES, TOOL_RATE_LIMIT_PER_MIN

log = logging.getLogger(__name__)


@dataclass
class PolicyDecision:
    """Result of a policy check."""
    allowed: bool
    reason: str = ""


# ── Denylist ──

_LEGACY_DENY_ALIASES = {
    "checkin_habit": "habit_progress",
    "query_habit": "habit_progress",
}


def _build_deny_set(value: str) -> set[str]:
    denied = {name.strip() for name in value.split(",") if name.strip()}
    for legacy_name, current_name in _LEGACY_DENY_ALIASES.items():
        if legacy_name not in denied:
            continue
        denied.add(current_name)
        log.warning(
            "TOOL_DENY_NAMES entry '%s' now conservatively disables '%s'; "
            "update the configured tool name.",
            legacy_name,
            current_name,
        )
    return denied


_deny_set = _build_deny_set(TOOL_DENY_NAMES)

# ── Rate Limiter ──

_lock = Lock()
_call_log: dict[str, list[float]] = {}  # {tool_name: [timestamps]}
_WINDOW_S = 60.0


def _check_rate(tool_name: str) -> bool:
    """Return True if within rate limit, False if exceeded."""
    now = time.time()
    with _lock:
        timestamps = _call_log.get(tool_name, [])
        # Prune old entries
        timestamps = [t for t in timestamps if now - t < _WINDOW_S]
        if len(timestamps) >= TOOL_RATE_LIMIT_PER_MIN:
            _call_log[tool_name] = timestamps
            return False
        timestamps.append(now)
        _call_log[tool_name] = timestamps
        return True


# ── Public API ──

def check(tool_name: str, user_id: int = 0) -> PolicyDecision:
    """Check if a tool call is allowed.

    Returns PolicyDecision with allowed=True/False and reason.
    """
    # Denylist
    if tool_name in _deny_set:
        log.warning("Tool DENIED (denylist): %s", tool_name)
        return PolicyDecision(allowed=False, reason=f"Tool '{tool_name}' is disabled.")

    # Rate limit
    if not _check_rate(tool_name):
        log.warning("Tool DENIED (rate limit): %s", tool_name)
        return PolicyDecision(
            allowed=False,
            reason=f"Tool '{tool_name}' rate limited ({TOOL_RATE_LIMIT_PER_MIN}/min).",
        )

    return PolicyDecision(allowed=True)


def filter_tools(tool_defs: list[dict]) -> list[dict]:
    """Remove denied tools from the tool definitions array.

    Call this before passing tools to the LLM to prevent it from
    ever seeing blocked tools.
    """
    if not _deny_set:
        return tool_defs
    return [
        t for t in tool_defs
        if t.get("function", {}).get("name") not in _deny_set
    ]
