"""Heartbeat schedules sovereign Main entries and owns no semantic judgment."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from mochi.config import (
    SILENCE_THRESHOLD_HOURS,
    TZ,
    logical_today,
)
from mochi.db import (
    _connect,
    get_last_user_message,
    get_last_user_message_time,
    log_heartbeat,
)
from mochi.heartbeat_runtime import (
    begin_delivery,
    checkpoint_text_delivery,
    checkpoint_visible_delivery,
    claim_run,
    complete_delivery,
    complete_without_delivery,
    ensure_daily_free_time_plan,
    entry_from_claim,
    expire_unusable_free_time_runs,
    in_free_time_window,
    get_schedulable_runs,
    last_delivered_free_time_at,
    owner_free_time_unavailable_cue,
    record_failure,
    recover_prior_tool_attempt,
    remove_delivered_component,
    should_skip_unavailable_slot,
    store_delivery_progress,
    store_prepared_result,
)
from mochi.main_runtime import DurableChatResult


log = logging.getLogger(__name__)
_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / ".heartbeat_state"

SLEEPING = "SLEEPING"
AWAKE = "AWAKE"
TRANSITIONING = "TRANSITIONING"
RESLEEP_WINDOW_HOURS = 6
HEARTBEAT_TICK_SECONDS = 30


def _effective(key: str):
    from mochi.admin.admin_db import get_system_config

    return get_system_config(key)


def _wake_earliest_hour() -> int:
    return int(_effective("WAKE_EARLIEST_HOUR"))


def _sleep_after_hour() -> int:
    return int(_effective("SLEEP_AFTER_HOUR"))


def _hour_in_half_open(hour: int, start: int, end: int) -> bool:
    """True if ``hour`` is in ``[start, end)`` on a 24-hour clock.

    When ``end < start`` the range wraps past midnight, e.g. 06:00–01:00.
    """
    hour %= 24
    start %= 24
    end %= 24
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _is_awake_hour(hour: int) -> bool:
    return _hour_in_half_open(hour, _wake_earliest_hour(), _sleep_after_hour())


def _is_rest_hour(hour: int) -> bool:
    return not _is_awake_hour(hour)


def _fallback_wake_due(hour: int) -> bool:
    """Auto-wake once the configured sleep window has ended."""
    return _is_awake_hour(hour)


def awake_period_start(now: datetime) -> datetime:
    """Start of the current awake interval; wraps when sleep crosses midnight."""
    wake = time(_wake_earliest_hour() % 24, 0)
    sleep_after = time(_sleep_after_hour() % 24, 0)
    local_now = now.astimezone(TZ) if now.tzinfo else now.replace(tzinfo=TZ)
    wraps = wake >= sleep_after
    if wraps and local_now.time() < sleep_after:
        start_date = local_now.date() - timedelta(days=1)
    else:
        start_date = local_now.date()
    return datetime.combine(start_date, wake, tzinfo=TZ)


def owner_spoken_since_awake(
    now: datetime, last_user_at: datetime | str | None,
) -> bool:
    """True if the owner messaged at or after this awake period started.

    Messages sent during rest hours fall before the period start, so they
    do not count as the owner being up.
    """
    parsed = last_user_at
    if isinstance(parsed, str):
        try:
            parsed = datetime.fromisoformat(parsed)
        except ValueError:
            return False
    if parsed is None:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ) >= awake_period_start(now)


def _persist_state(state: str, changed_at: datetime | None = None) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = (changed_at or datetime.now(TZ)).isoformat()
        _STATE_FILE.write_text(
            json.dumps({"state": state, "at": ts}),
            encoding="utf-8",
        )
    except Exception as exc:
        log.debug("Failed to persist heartbeat state: %s", exc)


def _init_state() -> str:
    now = datetime.now(TZ)
    try:
        if _STATE_FILE.exists():
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            saved = data.get("state")
            saved_at = datetime.fromisoformat(data["at"])
            if saved_at.tzinfo is None:
                saved_at = saved_at.replace(tzinfo=TZ)
            if (
                (now - saved_at).total_seconds() < 12 * 3600
                and saved in {SLEEPING, AWAKE, TRANSITIONING}
            ):
                return SLEEPING if saved == TRANSITIONING else saved
    except Exception as exc:
        log.debug("Failed to read persisted heartbeat state: %s", exc)
    return (
        AWAKE
        if _is_awake_hour(now.hour)
        else SLEEPING
    )


_state: str = _init_state()
_state_changed_at: datetime = datetime.now(TZ)
_wake_reason: str | None = None
_last_sleep_at: datetime | None = None
_silent_pause = False

_bedtime_callback = None
_weekly_callback = None
_core_refresh_callback = None
_core_refresh_busy = False
_runtime_prepare_callback = None
_runtime_delivery_callback = None
_runtime_transport = ""
_active_chat_tasks: set[asyncio.Task] = set()
_chat_activity_generation = 0


def set_bedtime_callback(callback) -> None:
    global _bedtime_callback
    _bedtime_callback = callback


def set_weekly_callback(callback) -> None:
    global _weekly_callback
    _weekly_callback = callback


def set_core_refresh_callback(callback) -> None:
    global _core_refresh_callback
    _core_refresh_callback = callback


def set_main_runtime_callbacks(prepare_callback, delivery_callback, transport: str) -> None:
    global _runtime_prepare_callback, _runtime_delivery_callback, _runtime_transport
    _runtime_prepare_callback = prepare_callback
    _runtime_delivery_callback = delivery_callback
    _runtime_transport = transport


def track_active_chat_task() -> None:
    """Mark the current owner-message task active through its final delivery."""
    global _chat_activity_generation
    task = asyncio.current_task()
    if task is None or task in _active_chat_tasks:
        return
    _active_chat_tasks.add(task)
    _chat_activity_generation += 1
    task.add_done_callback(_active_chat_tasks.discard)


def has_active_chat() -> bool:
    return any(not task.done() for task in _active_chat_tasks)


def chat_activity_generation() -> int:
    return _chat_activity_generation


def free_time_turn_available(chat_generation: int) -> bool:
    return (
        _state == AWAKE
        and not has_active_chat()
        and chat_generation == _chat_activity_generation
    )


def wake_up(reason: str = "unknown") -> None:
    global _state, _state_changed_at, _wake_reason
    if _state == SLEEPING:
        _state = AWAKE
        _state_changed_at = datetime.now(TZ)
        _wake_reason = reason
        _persist_state(AWAKE, _state_changed_at)


def go_to_sleep(reason: str = "unknown") -> None:
    global _state, _state_changed_at, _wake_reason, _last_sleep_at
    if _state in {AWAKE, TRANSITIONING}:
        _state = SLEEPING
        _state_changed_at = datetime.now(TZ)
        _last_sleep_at = _state_changed_at
        _wake_reason = None
        _persist_state(SLEEPING, _state_changed_at)
        log.info("SLEEPING - reason: %s", reason)


def claim_sleep_transition(trigger: str) -> bool:
    global _state, _state_changed_at
    if _state != AWAKE:
        return False
    _state = TRANSITIONING
    _state_changed_at = datetime.now(TZ)
    _persist_state(TRANSITIONING, _state_changed_at)
    log.info("Sleep transition claimed: %s", trigger)
    return True


def should_wake_on_message() -> bool:
    """Wake on owner chat only during awake hours.

    ``hour >= wake_earliest`` is wrong when sleep wraps midnight: a 23:00–07:00
    rest window would still treat 23:00 as wakeable because 23 >= 7.
    """
    return _state == SLEEPING and _is_awake_hour(datetime.now(TZ).hour)


def bedtime_tool_available() -> bool:
    if _state != AWAKE or not bedtime_entry_enabled():
        return False
    return _is_rest_hour(datetime.now(TZ).hour)


def bedtime_entry_enabled() -> bool:
    return bool(_effective("BEDTIME_ENTRY_ENABLED"))


def bedtime_entry_timeout() -> float:
    return float(_effective("BEDTIME_ENTRY_TIMEOUT_S"))


async def run_silent_bedtime(user_id: int, trigger: str) -> bool:
    if not claim_sleep_transition(trigger):
        return False
    try:
        if not bedtime_entry_enabled() or _bedtime_callback is None:
            return False
        delivered = bool(
            await asyncio.wait_for(
                _bedtime_callback(user_id, trigger),
                timeout=bedtime_entry_timeout(),
            )
        )
        if delivered:
            log_heartbeat(_state, "bedtime_entry", trigger)
        return delivered
    except asyncio.TimeoutError:
        log_heartbeat(_state, "bedtime_timeout", trigger)
        return False
    except Exception as exc:
        log.error("Bedtime entry failed: %s", exc, exc_info=True)
        log_heartbeat(_state, "bedtime_failure", str(exc)[:200])
        return False
    finally:
        go_to_sleep(f"{trigger}_detected")


def check_silence_sleep() -> dict | None:
    if _state != AWAKE:
        return None
    now = datetime.now(TZ)
    if not _is_rest_hour(now.hour):
        return None
    from mochi.config import OWNER_USER_ID as user_id

    if user_id is None:
        return None
    raw = get_last_user_message_time(user_id)
    if not raw:
        return None
    try:
        last = datetime.fromisoformat(raw)
        if last.tzinfo is None:
            last = last.replace(tzinfo=TZ)
        silence_hours = (now - last).total_seconds() / 3600
    except (TypeError, ValueError):
        return None
    if silence_hours < SILENCE_THRESHOLD_HOURS:
        return None
    is_resleep = bool(
        _last_sleep_at
        and (now - _last_sleep_at).total_seconds() < RESLEEP_WINDOW_HOURS * 3600
    )
    return {
        "context_hint": "re_sleep" if is_resleep else "first_sleep",
        "silence_hours": round(silence_hours, 1),
    }


def enter_silent_pause() -> None:
    global _silent_pause
    _silent_pause = True


def clear_silent_pause() -> None:
    global _silent_pause
    _silent_pause = False


def _check_silence_pause() -> None:
    from mochi.config import OWNER_USER_ID as user_id

    if user_id is None:
        return
    raw = get_last_user_message_time(user_id)
    if not raw:
        return
    try:
        last = datetime.fromisoformat(raw)
        if last.tzinfo is None:
            last = last.replace(tzinfo=TZ)
        silence_hours = (datetime.now(TZ) - last).total_seconds() / 3600
    except (TypeError, ValueError):
        return
    if silence_hours >= float(_effective("SILENCE_PAUSE_DAYS")) * 24:
        enter_silent_pause()
    elif _silent_pause:
        clear_silent_pause()


def get_stats() -> dict:
    now = datetime.now(TZ)
    start = datetime.combine(now.date(), datetime.min.time(), tzinfo=TZ).astimezone(
        timezone.utc,
    )
    end = start + timedelta(days=1)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS count, MAX(handled_at) AS last_think_at "
            "FROM heartbeat_runs WHERE entry_kind = 'free_time' "
            "AND attempt_count > 0 AND created_at >= ? AND created_at < ?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
    finally:
        conn.close()
    return {
        "state": _state,
        "state_changed_at": _state_changed_at.isoformat(),
        "free_time_thoughts_today": int(row["count"] or 0),
        "free_time_thought_limit": _effective("MAX_DAILY_FREE_TIME"),
        "last_think_at": row["last_think_at"],
        "wake_reason": _wake_reason,
    }


async def _run_maintenance_if_due(
    user_id: int,
    now: datetime | None = None,
) -> bool:
    if not _effective("MAINTENANCE_ENABLED"):
        return False
    now = now or datetime.now(TZ)
    period = logical_today(now)
    if now.hour < _effective("MAINTENANCE_HOUR"):
        return False
    from mochi.db import claim_scheduled_run, finish_scheduled_run

    if not claim_scheduled_run("nightly", period):
        return False
    try:
        import mochi.skills as skill_registry
        from mochi.skills.base import SkillContext

        skill = skill_registry.get_skill("maintenance")
        if skill is None:
            raise RuntimeError("Maintenance skill not found")
        result = await skill.run(SkillContext(trigger="cron", user_id=user_id))
        if not result.success:
            raise RuntimeError(result.output or "Maintenance skill failed")
    except Exception as exc:
        finish_scheduled_run("nightly", period, success=False, error=str(exc))
        log_heartbeat(_state, "maintenance_error", str(exc)[:200])
        return True
    finish_scheduled_run("nightly", period, success=True)
    log_heartbeat(_state, "maintenance", result.output[:200])
    return True


async def _run_weekly_if_due(
    user_id: int,
    now: datetime | None = None,
) -> bool:
    if not _effective("WEEKLY_MAINTENANCE_ENABLED"):
        return False
    now = now or datetime.now(TZ)
    logical_date = logical_today(now)
    logical_day = datetime.strptime(logical_date, "%Y-%m-%d").date()
    if logical_day.weekday() != 0:
        return False
    maintenance_hour = _effective("MAINTENANCE_HOUR")
    weekly_minute = _effective("WEEKLY_MAINTENANCE_MINUTE")
    if now.hour < maintenance_hour or (
        now.hour == maintenance_hour and now.minute < weekly_minute
    ):
        return False
    from mochi.db import (
        claim_scheduled_run,
        finish_scheduled_run,
        get_scheduled_run,
    )

    nightly = get_scheduled_run("nightly", logical_date)
    if not nightly or nightly["status"] != "success":
        return False
    iso = logical_day.isocalendar()
    period_key = f"{iso.year}-W{iso.week:02d}"
    if not claim_scheduled_run("weekly", period_key):
        return False
    try:
        if _weekly_callback is None:
            raise RuntimeError("Weekly Main callback is not registered")
        await asyncio.wait_for(
            _weekly_callback(user_id, logical_date, period_key),
            timeout=_effective("LLM_HEARTBEAT_TIMEOUT_SECONDS"),
        )
    except Exception as exc:
        finish_scheduled_run("weekly", period_key, success=False, error=str(exc))
        log_heartbeat(_state, "weekly_error", str(exc)[:200])
        return True
    finish_scheduled_run("weekly", period_key, success=True)
    log_heartbeat(_state, "weekly", period_key)
    return True


CORE_REFRESH_DEFAULT_HOURS = (12, 23)


def parse_core_refresh_hours(raw) -> tuple[int, ...]:
    """Parse `CORE_REFRESH_HOURS` as unique clock hours in 0–23."""
    hours: list[int] = []
    seen: set[int] = set()
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hour = int(part)
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23 and hour not in seen:
            seen.add(hour)
            hours.append(hour)
    return tuple(hours or CORE_REFRESH_DEFAULT_HOURS)


def minutes_into_logical_day(hour: int, minute: int, maintenance_hour: int) -> int:
    """Minutes since MAINTENANCE_HOUR on a 24h clock that wraps past midnight."""
    hour %= 24
    minute = min(max(int(minute), 0), 59)
    maintenance_hour %= 24
    if hour >= maintenance_hour:
        return (hour - maintenance_hour) * 60 + minute
    return (24 - maintenance_hour + hour) * 60 + minute


def scheduled_hour_reached(
    now: datetime,
    hour: int,
    *,
    maintenance_hour: int | None = None,
) -> bool:
    """True once ``hour:00`` has occurred on the current logical day."""
    mh = int(
        _effective("MAINTENANCE_HOUR") if maintenance_hour is None else maintenance_hour
    )
    return minutes_into_logical_day(
        now.hour, now.minute, mh,
    ) >= minutes_into_logical_day(hour, 0, mh)


def format_core_refresh_ack(result) -> str:
    if getattr(result, "successful_effects", False):
        return "Core 已整理。"
    return "Core 看过了，没有需要改的。"


async def _invoke_core_refresh(
    user_id: int,
    logical_date: str,
    period_key: str,
):
    global _core_refresh_busy
    if _core_refresh_busy:
        raise RuntimeError("Core 正在整理，请稍后再试")
    if _core_refresh_callback is None:
        raise RuntimeError("Core refresh callback is not registered")
    _core_refresh_busy = True
    try:
        return await asyncio.wait_for(
            _core_refresh_callback(user_id, logical_date, period_key),
            timeout=_effective("LLM_HEARTBEAT_TIMEOUT_SECONDS"),
        )
    finally:
        _core_refresh_busy = False


async def run_core_refresh_now(user_id: int):
    """Owner-triggered Core refresh; does not consume a scheduled slot."""
    now = datetime.now(TZ)
    logical_date = logical_today(now)
    period_key = f"force-{now.strftime('%Y%m%dT%H%M%S')}"
    result = await _invoke_core_refresh(user_id, logical_date, period_key)
    log_heartbeat(_state, "core_refresh_force", period_key)
    return result


async def _run_core_refresh_if_due(
    user_id: int,
    now: datetime | None = None,
) -> bool:
    if not _effective("CORE_REFRESH_ENABLED"):
        return False
    now = now or datetime.now(TZ)
    logical_date = logical_today(now)
    hours = parse_core_refresh_hours(_effective("CORE_REFRESH_HOURS"))
    ran = False
    from mochi.db import claim_scheduled_run, finish_scheduled_run

    for hour in hours:
        if not scheduled_hour_reached(now, hour):
            continue
        if _core_refresh_busy:
            continue
        period_key = f"{logical_date}-{hour:02d}"
        if not claim_scheduled_run("core_refresh", period_key):
            continue
        try:
            await _invoke_core_refresh(user_id, logical_date, period_key)
        except Exception as exc:
            finish_scheduled_run(
                "core_refresh", period_key, success=False, error=str(exc),
            )
            log_heartbeat(_state, "core_refresh_error", str(exc)[:200])
            ran = True
            continue
        finish_scheduled_run("core_refresh", period_key, success=True)
        log_heartbeat(_state, "core_refresh", period_key)
        ran = True
    return ran


async def _run_relationship_morning_if_due(
    user_id: int,
    now: datetime | None = None,
) -> bool:
    """Silent daily relationship assessment after RELATIONSHIP_MORNING_HOUR."""
    if not _effective("RELATIONSHIP_MORNING_ENABLED"):
        return False
    now = now or datetime.now(TZ)
    if now.hour < int(_effective("RELATIONSHIP_MORNING_HOUR")):
        return False
    from mochi.config import logical_today
    from mochi.db import claim_scheduled_run, finish_scheduled_run

    period = logical_today(now)
    if not claim_scheduled_run("relationship_morning", period):
        return False
    try:
        import mochi.skills as skill_registry
        from mochi.skills.base import SkillContext

        skill = skill_registry.get_skill("relationship_health")
        if skill is None:
            raise RuntimeError("Relationship health skill not found")
        result = await asyncio.wait_for(
            skill.run(SkillContext(trigger="cron", user_id=user_id)),
            timeout=_effective("LLM_HEARTBEAT_TIMEOUT_SECONDS"),
        )
        if not result.success:
            raise RuntimeError(result.output or "Morning relationship assessment failed")
    except Exception as exc:
        finish_scheduled_run(
            "relationship_morning", period, success=False, error=str(exc),
        )
        log_heartbeat(_state, "relationship_morning_error", str(exc)[:200])
        return True
    finish_scheduled_run("relationship_morning", period, success=True)
    log_heartbeat(_state, "relationship_morning", (result.summary or result.output)[:200])
    return True


async def _prepare_autonomous(claimed: dict) -> DurableChatResult | None:
    if claimed.get("result_json"):
        durable = DurableChatResult.from_json(claimed["result_json"])
        complete_without_delivery(claimed, durable, "stale")
        log_heartbeat(_state, "free_time_stale")
        return None
    recovered = recover_prior_tool_attempt(claimed)
    if recovered is not None:
        complete_without_delivery(claimed, recovered, "tools_only")
        log_heartbeat(_state, f"{claimed['entry_kind']}_tools_only", "recovered")
        return None
    if _runtime_prepare_callback is None:
        record_failure(claimed, "Main runtime callback is not registered")
        return None
    try:
        result = await asyncio.wait_for(
            _runtime_prepare_callback(entry_from_claim(claimed)),
            timeout=_effective("LLM_HEARTBEAT_TIMEOUT_SECONDS"),
        )
        durable = result.to_durable()
    except asyncio.TimeoutError:
        record_failure(claimed, "Main runtime timed out")
        log_heartbeat(_state, f"{claimed['entry_kind']}_timeout")
        return None
    except Exception as exc:
        record_failure(claimed, f"Main failed: {exc}")
        log_heartbeat(
            _state, f"{claimed['entry_kind']}_failure", str(exc)[:200],
        )
        return None
    if durable.disposition == "skip" and not durable.successful_effects:
        complete_without_delivery(claimed, durable, "no_effect")
        log_heartbeat(_state, "free_time_no_effect")
        return None
    if durable.disposition == "handled" and durable.successful_effects:
        complete_without_delivery(claimed, durable, "tools_only")
        log_heartbeat(_state, f"{claimed['entry_kind']}_tools_only")
        return None
    if durable.disposition != "deliver" or not (
        durable.text or durable.stickers or durable.images
    ):
        complete_without_delivery(claimed, durable, "no_effect")
        log_heartbeat(_state, "free_time_no_effect")
        return None
    if not store_prepared_result(claimed, durable):
        return None
    claimed["status"] = "ready"
    claimed["result_json"] = durable.to_json()
    claimed["last_error"] = ""
    return durable


async def _deliver_autonomous(
    claimed: dict,
    durable: DurableChatResult,
) -> bool:
    if _runtime_delivery_callback is None:
        complete_without_delivery(claimed, durable, "delivery_failed")
        log_heartbeat(_state, "free_time_delivery_failed")
        return False
    if (
        has_active_chat()
        or claimed.get("_chat_activity_generation") != chat_activity_generation()
    ):
        complete_without_delivery(claimed, durable, "active_chat")
        log_heartbeat(_state, "free_time_active_chat")
        return False
    if _state != AWAKE and not in_free_time_window(datetime.now(TZ)):
        complete_without_delivery(claimed, durable, "asleep")
        log_heartbeat(_state, "free_time_asleep")
        return False
    if not begin_delivery(claimed):
        return False
    from mochi.ai_client import ChatResult

    remaining = durable
    components = []
    if remaining.text:
        components.append(("text", remaining.text))
    components.extend(("sticker", item) for item in remaining.stickers)
    components.extend(("image", item) for item in remaining.images)
    for kind, value in components:
        if not free_time_turn_available(
            int(claimed.get("_chat_activity_generation") or 0)
        ):
            complete_without_delivery(claimed, remaining, "active_chat")
            log_heartbeat(_state, "free_time_active_chat")
            return False
        if kind == "text":
            component = ChatResult(text=value)
        elif kind == "sticker":
            component = ChatResult(stickers=[value])
        else:
            component = ChatResult(images=[value])
        try:
            delivered = await _runtime_delivery_callback(
                claimed["channel_id"], component,
            )
        except Exception as exc:
            complete_without_delivery(claimed, durable, "delivery_unknown")
            log_heartbeat(_state, "free_time_delivery_unknown", str(exc)[:200])
            return False
        if not delivered:
            complete_without_delivery(claimed, durable, "delivery_failed")
            log_heartbeat(_state, "free_time_delivery_failed")
            return False
        if kind == "text":
            checkpointed = checkpoint_text_delivery(
                claimed,
                content=value,
                entry_kind=claimed["entry_kind"],
            )
        else:
            checkpointed = checkpoint_visible_delivery(claimed)
        if not checkpointed:
            return False
        remaining = remove_delivered_component(remaining, kind, value)
        if not store_delivery_progress(claimed, remaining):
            return False

    result = ChatResult.from_durable(durable)
    if durable.pending_history and not result.confirm_delivered():
        record_failure(claimed, "delivered result history was not confirmed")
        return False
    if not complete_delivery(claimed):
        return False
    log_heartbeat(
        _state, f"{claimed['entry_kind']}_delivered", durable.text[:100],
    )
    return True


async def _run_claimed_entry(claimed: dict) -> None:
    durable = await _prepare_autonomous(claimed)
    if durable is None:
        return
    await _deliver_autonomous(claimed, durable)


async def run_main_runtime_tick(
    user_id: int,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Refresh observer caches and run due Free Time claims."""
    now = now or datetime.now(TZ)
    if _silent_pause:
        return []
    if _state != AWAKE and not in_free_time_window(now):
        return []
    from mochi.observers import collect_all

    await collect_all()
    created = ensure_daily_free_time_plan(
        user_id=user_id,
        channel_id=user_id,
        transport=_runtime_transport,
        now=now,
        max_daily=int(_effective("MAX_DAILY_FREE_TIME")),
    )
    active_chat = has_active_chat()
    expire_unusable_free_time_runs(
        now=now,
        active_chat=active_chat,
        awake=True,
    )
    if active_chat:
        return created
    last_user = get_last_user_message(user_id)
    last_user_at = None if last_user is None else last_user.get("created_at")
    unavailable_cue = owner_free_time_unavailable_cue(
        sleeping=_state == SLEEPING,
        last_user_text=None if last_user is None else last_user.get("content"),
        owner_spoken_since_wake=owner_spoken_since_awake(now, last_user_at),
    )
    last_delivered_at = last_delivered_free_time_at(user_id)
    quiet_since = (
        awake_period_start(now) if unavailable_cue == "quiet_wake" else None
    )
    for row in get_schedulable_runs(now=now):
        current_now = datetime.now(TZ)
        in_window = in_free_time_window(current_now)
        expire_unusable_free_time_runs(
            now=current_now,
            active_chat=has_active_chat(),
            awake=_state == AWAKE or in_window,
        )
        if has_active_chat():
            break
        if _state != AWAKE and not in_window:
            break
        claimed = claim_run(row["run_key"], now=current_now)
        if claimed is not None:
            skip_reason = should_skip_unavailable_slot(
                now=current_now,
                cue=unavailable_cue,
                last_delivered_at=last_delivered_at,
                since=quiet_since,
            )
            if skip_reason:
                complete_without_delivery(
                    claimed,
                    DurableChatResult(disposition="skip"),
                    f"skipped_{skip_reason}",
                )
                log_heartbeat(_state, f"free_time_skipped_{skip_reason}")
                break
            claimed["_chat_activity_generation"] = chat_activity_generation()
            await _run_claimed_entry(claimed)
            break
    return created


async def heartbeat_loop() -> None:
    log.info(
        "Heartbeat started: internal_tick=%ds, state=%s",
        HEARTBEAT_TICK_SECONDS,
        _state,
    )
    while True:
        interval = HEARTBEAT_TICK_SECONDS
        try:
            from mochi.config import OWNER_USER_ID as user_id

            if user_id is None:
                await asyncio.sleep(interval)
                continue
            now = datetime.now(TZ)
            await _run_maintenance_if_due(user_id, now)
            await _run_weekly_if_due(user_id, now)
            await _run_core_refresh_if_due(user_id, now)
            await _run_relationship_morning_if_due(user_id, now)
            ensure_daily_free_time_plan(
                user_id=user_id,
                channel_id=user_id,
                transport=_runtime_transport,
                now=now,
                max_daily=int(_effective("MAX_DAILY_FREE_TIME")),
            )
            if _state == TRANSITIONING:
                log_heartbeat(_state, "sleep_transition")
                await asyncio.sleep(interval)
                continue
            if _state == SLEEPING:
                in_window = in_free_time_window(now)
                if not in_window:
                    expire_unusable_free_time_runs(
                        now=now,
                        active_chat=has_active_chat(),
                        awake=False,
                    )
                if _fallback_wake_due(now.hour):
                    wake_up(f"sleep_end_{_wake_earliest_hour():02d}:00")
                elif not in_window:
                    log_heartbeat(_state, "sleeping")
                    await asyncio.sleep(interval)
                    continue
            sleep_action = check_silence_sleep()
            if sleep_action:
                trigger = (
                    "resleep"
                    if sleep_action["context_hint"] == "re_sleep"
                    else "silence"
                )
                await run_silent_bedtime(user_id, trigger)
                await asyncio.sleep(interval)
                continue
            _check_silence_pause()
            if _silent_pause:
                log_heartbeat(_state, "silent_pause")
                await asyncio.sleep(interval)
                continue
            await run_main_runtime_tick(user_id, now=now)
        except Exception as exc:
            log.error("Heartbeat error: %s", exc, exc_info=True)
            log_heartbeat(_state, "error", str(exc)[:200])
        await asyncio.sleep(interval)
