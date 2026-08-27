"""Heartbeat schedules sovereign Main entries and owns no semantic judgment."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mochi.config import TZ, logical_today
from mochi.db import (
    _connect,
    get_last_user_message_time,
    log_heartbeat,
)
from mochi.heartbeat_runtime import (
    begin_delivery,
    checkpoint_delivery,
    claim_run,
    complete_delivery,
    complete_without_delivery,
    ensure_daily_free_time_plan,
    entry_from_claim,
    expire_unusable_free_time_runs,
    get_schedulable_runs,
    recover_prior_tool_attempt,
    store_prepared_result,
)
from mochi.main_runtime import DurableChatResult


log = logging.getLogger(__name__)
_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / ".heartbeat_state"

SLEEPING = "SLEEPING"
AWAKE = "AWAKE"
TRANSITIONING = "TRANSITIONING"
RESLEEP_WINDOW_HOURS = 6
FREE_TIME_CHAT_QUIET_MINUTES = 30
HEARTBEAT_TICK_SECONDS = 300
WAKE_EARLIEST_HOUR = 6
SLEEP_AFTER_HOUR = 21
SILENCE_PAUSE_DAYS = 3.0
SILENCE_THRESHOLD_HOURS = 1.0
FALLBACK_WAKE_HOUR = 10
BEDTIME_ENTRY_TIMEOUT_SECONDS = 60
MAIN_RUNTIME_TIMEOUT_SECONDS = 120
MAINTENANCE_HOUR = 3
WEEKLY_MAINTENANCE_MINUTE = 15


def _max_daily_free_time_opportunities() -> int:
    from mochi.admin.admin_db import get_system_config

    return max(
        0,
        int(get_system_config("MAX_DAILY_FREE_TIME_OPPORTUNITIES")),
    )


def _wake_earliest_hour() -> int:
    return WAKE_EARLIEST_HOUR


def _sleep_after_hour() -> int:
    return SLEEP_AFTER_HOUR


def _is_awake_hour(hour: int) -> bool:
    return _wake_earliest_hour() <= hour < _sleep_after_hour()


def _is_rest_hour(hour: int) -> bool:
    return not _is_awake_hour(hour)


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
_runtime_prepare_callback = None
_runtime_delivery_callback = None
_runtime_transport = ""


def set_bedtime_callback(callback) -> None:
    global _bedtime_callback
    _bedtime_callback = callback


def set_weekly_callback(callback) -> None:
    global _weekly_callback
    _weekly_callback = callback


def set_main_runtime_callbacks(prepare_callback, delivery_callback, transport: str) -> None:
    global _runtime_prepare_callback, _runtime_delivery_callback, _runtime_transport
    _runtime_prepare_callback = prepare_callback
    _runtime_delivery_callback = delivery_callback
    _runtime_transport = transport


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
    return (
        _state == SLEEPING
        and datetime.now(TZ).hour >= _wake_earliest_hour()
    )


def bedtime_tool_available() -> bool:
    if _state != AWAKE or not bedtime_entry_enabled():
        return False
    return _is_rest_hour(datetime.now(TZ).hour)


def bedtime_entry_enabled() -> bool:
    return True


def bedtime_entry_timeout() -> float:
    return BEDTIME_ENTRY_TIMEOUT_SECONDS


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


def _free_time_quiet_until(user_id: int, now: datetime) -> datetime | None:
    raw = get_last_user_message_time(user_id)
    if not raw:
        return None
    try:
        last = datetime.fromisoformat(raw)
        if last.tzinfo is None:
            last = last.replace(tzinfo=TZ)
    except (TypeError, ValueError):
        return None
    quiet_until = last + timedelta(minutes=FREE_TIME_CHAT_QUIET_MINUTES)
    return quiet_until if quiet_until > now else None


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
    if silence_hours >= SILENCE_PAUSE_DAYS * 24:
        enter_silent_pause()
    elif _silent_pause:
        clear_silent_pause()


def get_stats() -> dict:
    now = datetime.now(TZ)
    day = now.astimezone(TZ).date().isoformat()
    start = datetime.strptime(day, "%Y-%m-%d").replace(
        tzinfo=TZ,
    ).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    conn = _connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM heartbeat_runs WHERE text_delivered_at >= ? "
            "AND text_delivered_at < ?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "state": _state,
        "state_changed_at": _state_changed_at.isoformat(),
        "proactive_today": count,
        "proactive_limit": _max_daily_free_time_opportunities(),
        "wake_reason": _wake_reason,
    }


async def _run_maintenance_if_due(
    user_id: int,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(TZ)
    period = logical_today(now)
    if now.hour < MAINTENANCE_HOUR:
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
    now = now or datetime.now(TZ)
    logical_date = logical_today(now)
    logical_day = datetime.strptime(logical_date, "%Y-%m-%d").date()
    if logical_day.weekday() != 0:
        return False
    maintenance_hour = MAINTENANCE_HOUR
    weekly_minute = WEEKLY_MAINTENANCE_MINUTE
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
            timeout=MAIN_RUNTIME_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        finish_scheduled_run("weekly", period_key, success=False, error=str(exc))
        log_heartbeat(_state, "weekly_error", str(exc)[:200])
        return True
    finish_scheduled_run("weekly", period_key, success=True)
    log_heartbeat(_state, "weekly", period_key)
    return True


async def _prepare_autonomous(claimed: dict) -> DurableChatResult | None:
    recovered = recover_prior_tool_attempt(claimed)
    if recovered is not None:
        outcome = "tools_only" if recovered.successful_effects else "no_effect"
        complete_without_delivery(claimed, recovered, outcome)
        log_heartbeat(_state, f"free_time_{outcome}", "recovered")
        return None
    if _runtime_prepare_callback is None:
        complete_without_delivery(
            claimed,
            DurableChatResult(disposition="invalid"),
            "failure",
        )
        return None
    try:
        result = await asyncio.wait_for(
            _runtime_prepare_callback(entry_from_claim(claimed)),
            timeout=MAIN_RUNTIME_TIMEOUT_SECONDS,
        )
        durable = result.to_durable()
    except asyncio.TimeoutError:
        complete_without_delivery(
            claimed,
            DurableChatResult(disposition="invalid"),
            "timeout",
        )
        log_heartbeat(_state, "free_time_timeout")
        return None
    except Exception as exc:
        complete_without_delivery(
            claimed,
            DurableChatResult(disposition="invalid"),
            "failure",
        )
        log_heartbeat(
            _state, "free_time_failure", str(exc)[:200],
        )
        return None
    if durable.disposition == "skip" and not durable.successful_effects:
        complete_without_delivery(claimed, durable, "no_effect")
        log_heartbeat(_state, "free_time_no_effect")
        return None
    if durable.disposition == "handled" and durable.successful_effects:
        complete_without_delivery(claimed, durable, "tools_only")
        log_heartbeat(_state, "free_time_tools_only")
        return None
    if durable.disposition != "deliver" or not (
        durable.text or durable.stickers
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
    if not begin_delivery(claimed):
        return False
    from mochi.ai_client import ChatResult

    result = ChatResult.from_durable(durable)
    try:
        delivered = await _runtime_delivery_callback(
            claimed["channel_id"],
            result,
        )
    except Exception as exc:
        complete_without_delivery(claimed, durable, "delivery_unknown")
        log_heartbeat(_state, "free_time_delivery_unknown", str(exc)[:200])
        return False
    if not delivered:
        complete_without_delivery(claimed, durable, "delivery_failed")
        log_heartbeat(_state, "free_time_delivery_failed")
        return False
    content = durable.text or "[贴纸]"
    if not checkpoint_delivery(claimed, content=content):
        return False
    if not complete_delivery(claimed):
        return False
    log_heartbeat(
        _state, "free_time_delivered", durable.text[:100],
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
    """Refresh caches and run due one-shot Free Time opportunities."""
    if _state != AWAKE or _silent_pause:
        return []
    now = now or datetime.now(TZ)
    from mochi.free_time import choose_card_for_run
    from mochi.observers import collect_all

    await collect_all()
    try:
        from mochi.diary import refresh_diary_status

        refresh_diary_status(user_id)
    except Exception as exc:
        log.warning("Diary status refresh failed: %s", exc)
    created = ensure_daily_free_time_plan(
        user_id=user_id,
        channel_id=user_id,
        transport=_runtime_transport,
        now=now,
        max_daily=_max_daily_free_time_opportunities(),
    )
    active_chat = _free_time_quiet_until(user_id, now) is not None
    expire_unusable_free_time_runs(
        now=now,
        active_chat=active_chat,
        awake=True,
    )
    if active_chat:
        return created
    for row in get_schedulable_runs(now=now):
        choose_card_for_run(
            row["run_key"],
            user_id=user_id,
            now=now,
        )
        claimed = claim_run(row["run_key"], now=now)
        if claimed is not None:
            await _run_claimed_entry(claimed)
    return created


async def heartbeat_loop() -> None:
    log.info(
        "Heartbeat started: interval=%ds, state=%s",
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
            if _state == TRANSITIONING:
                log_heartbeat(_state, "sleep_transition")
                await asyncio.sleep(interval)
                continue
            if _state == SLEEPING:
                from mochi.observers import collect_all

                await collect_all()
                fallback_hour = FALLBACK_WAKE_HOUR
                if fallback_hour <= now.hour < _sleep_after_hour():
                    wake_up(f"fallback_{fallback_hour}:00")
                else:
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
