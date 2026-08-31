"""Deterministic opt-in adaptation from on-demand to routed tool loading."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import logging

from mochi.config import TZ


log = logging.getLogger(__name__)

PROMOTION_TURNS = 3
PROMOTION_WINDOW_DAYS = 30
REVERSION_UNUSED_DAYS = 30
MIN_TENURE_DAYS = 7

TOOL_ALIASES = {
    "query_habit": "habit_progress",
    "checkin_habit": "habit_progress",
}

_state_cache: dict[str, dict] | None = None
_state_cache_db: str | None = None


def reload_state() -> dict[str, dict]:
    """Reload persisted adaptive state after Nightly or an owner override."""
    global _state_cache, _state_cache_db
    try:
        from mochi.db import DB_PATH, get_adaptive_tool_load_states

        _state_cache = get_adaptive_tool_load_states()
        _state_cache_db = str(DB_PATH)
    except Exception:
        log.debug("Adaptive tool state unavailable", exc_info=True)
        _state_cache = {}
        _state_cache_db = None
    return _state_cache


def _states() -> dict[str, dict]:
    from mochi.db import DB_PATH

    if _state_cache is None or _state_cache_db != str(DB_PATH):
        return reload_state()
    return _state_cache


def resolve_definition(definition: dict) -> dict:
    """Return one definition with its single effective load attached."""
    resolved = deepcopy(definition)
    declared = str(definition.get("_load") or "on_demand")
    tool_name = str(definition.get("function", {}).get("name") or "")
    adaptive = bool(definition.get("_adaptive_load"))
    state = _states().get(tool_name, {}) if adaptive else {}
    effective = str(state.get("effective_load") or declared)
    if effective not in {"on_demand", "routed"} or not adaptive:
        effective = declared
    resolved["_declared_load"] = declared
    resolved["_load"] = effective
    resolved["_adaptive_load"] = adaptive
    resolved["_load_reason"] = str(
        state.get("reason")
        or ("fixed by skill contract" if not adaptive else "using declared load")
    )
    resolved["_load_changed_at"] = state.get("changed_at")
    resolved["_load_pinned"] = state.get("pinned_load")
    return resolved


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)


def recalculate(
    definitions: list[dict],
    *,
    now: datetime | None = None,
) -> dict[str, dict]:
    """Recalculate opted-in tools from successful distinct chat turns."""
    from mochi.db import (
        get_adaptive_tool_load_states,
        get_successful_chat_tool_turn_counts,
        save_adaptive_tool_load_state,
    )

    current_now = (now or datetime.now(TZ)).astimezone(TZ)
    cutoff = current_now - timedelta(days=PROMOTION_WINDOW_DAYS)
    counts = get_successful_chat_tool_turn_counts(
        since=cutoff.isoformat(),
        aliases=TOOL_ALIASES,
    )
    states = get_adaptive_tool_load_states()
    results: dict[str, dict] = {}

    for definition in definitions:
        if not definition.get("_adaptive_load"):
            continue
        tool_name = str(definition.get("function", {}).get("name") or "")
        declared = str(
            definition.get("_declared_load")
            or definition.get("_load")
            or ""
        )
        if not tool_name or declared != "on_demand":
            continue
        previous = states.get(tool_name, {})
        effective = str(previous.get("effective_load") or declared)
        pinned = previous.get("pinned_load")
        changed_at = _parse_time(previous.get("changed_at")) or current_now
        used_turns = counts.get(tool_name, 0)

        if pinned in {"on_demand", "routed"}:
            desired = pinned
            reason = f"pinned by user to {pinned}"
        elif effective == "routed":
            tenure = current_now - changed_at
            if tenure >= timedelta(days=MIN_TENURE_DAYS) and used_turns == 0:
                desired = declared
                reason = (
                    f"returned to {declared} after "
                    f"{REVERSION_UNUSED_DAYS} unused days"
                )
            else:
                desired = "routed"
                reason = (
                    f"kept routed; {used_turns} successful chat turn(s) "
                    f"in {PROMOTION_WINDOW_DAYS} days"
                )
        elif used_turns >= PROMOTION_TURNS:
            desired = "routed"
            reason = (
                f"promoted after {used_turns} successful chat turn(s) "
                f"in {PROMOTION_WINDOW_DAYS} days"
            )
        else:
            desired = declared
            reason = (
                f"using {declared}; {used_turns}/{PROMOTION_TURNS} "
                "successful chat turns"
            )

        if desired != effective:
            changed_at = current_now
        changed = desired != effective
        save_adaptive_tool_load_state(
            tool_name,
            effective_load=desired,
            changed_at=changed_at.isoformat(),
            pinned_load=pinned,
            reason=reason,
        )
        results[tool_name] = {
            "declared_load": declared,
            "effective_load": desired,
            "pinned_load": pinned,
            "used_turns": used_turns,
            "reason": reason,
            "changed": changed,
        }

    reload_state()
    return results


def pin_definition(
    definition: dict,
    pinned_load: str | None,
    *,
    now: datetime | None = None,
) -> dict:
    """Pin or unpin one opted-in definition without changing its contract."""
    from mochi.db import get_adaptive_tool_load_states, save_adaptive_tool_load_state

    if not definition.get("_adaptive_load"):
        raise ValueError("tool is fixed by its skill contract")
    if pinned_load not in {None, "on_demand", "routed"}:
        raise ValueError("load must be on_demand, routed, or null")
    tool_name = str(definition.get("function", {}).get("name") or "")
    declared = str(
        definition.get("_declared_load")
        or definition.get("_load")
        or ""
    )
    if not tool_name or declared != "on_demand":
        raise ValueError("adaptive tool contract is invalid")
    current_now = (now or datetime.now(TZ)).astimezone(TZ)
    previous = get_adaptive_tool_load_states().get(tool_name, {})
    previous_effective = str(previous.get("effective_load") or declared)
    previous_pin = previous.get("pinned_load")
    effective = (
        declared
        if pinned_load is None and previous_pin in {"on_demand", "routed"}
        else pinned_load or previous_effective
    )
    changed = (
        previous_pin != pinned_load
        or previous_effective != effective
    )
    effective_changed = previous_effective != effective
    changed_at = (
        current_now
        if effective_changed
        else _parse_time(previous.get("changed_at")) or current_now
    )
    reason = (
        f"pinned by user to {pinned_load}"
        if pinned_load
        else "user pin reset; Nightly will recalculate"
    )
    save_adaptive_tool_load_state(
        tool_name,
        effective_load=effective,
        changed_at=changed_at.isoformat(),
        pinned_load=pinned_load,
        reason=reason,
    )
    reload_state()
    return {
        "tool_name": tool_name,
        "declared_load": declared,
        "effective_load": effective,
        "pinned_load": pinned_load,
        "reason": reason,
        "changed": changed,
    }
