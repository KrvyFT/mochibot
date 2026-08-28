"""Durable scheduling and delivery state for Main heartbeat entries."""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone

from mochi.config import TZ
from mochi.db import _connect, get_tool_executions_for_turn
from mochi.main_runtime import DurableChatResult, MainRuntimeEntry


UTC = timezone.utc
FREE_TIME_AWAKE_START = time(6, 0)
FREE_TIME_AWAKE_END = time(21, 0)
FREE_TIME_ACTIVATION_CHANCE = 0.6
FREE_TIME_SEARCH_SHARE = 0.2
FREE_TIME_MISSED_GRACE = timedelta(seconds=45)
_LEASE_SECONDS = 300


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _as_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(UTC)


def _day_prefix(local_date: str) -> str:
    return f"free_time:{local_date}:%"


def _select_search_run_keys(
    run_keys: list[str],
    rng: random.Random | random.SystemRandom,
) -> set[str]:
    count = min(len(run_keys), int(len(run_keys) * FREE_TIME_SEARCH_SHARE + 0.5))
    return set(rng.sample(run_keys, count)) if count else set()


def ensure_daily_free_time_plan(
    *,
    user_id: int,
    channel_id: int,
    transport: str,
    now: datetime,
    max_daily: int,
    rng: random.Random | random.SystemRandom | None = None,
) -> list[str]:
    """Persist today's random Free Time opportunities exactly once."""
    rng = rng or random.SystemRandom()
    local_now = now.astimezone(TZ)
    local_date = local_now.date().isoformat()
    now_iso = _iso(local_now)
    max_daily = max(0, min(10, int(max_daily)))
    start = datetime.combine(
        local_now.date(), FREE_TIME_AWAKE_START, tzinfo=TZ,
    )
    end = datetime.combine(local_now.date(), FREE_TIME_AWAKE_END, tzinfo=TZ)
    marker = (
        f"{local_date}:{max_daily}:{user_id}:{channel_id}:{transport}"
    )
    created: list[str] = []

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', outcome = 'expired', "
            "handled_at = ?, claim_token = NULL, lease_until = NULL, "
            "next_attempt_at = NULL, last_error = '' "
            "WHERE entry_kind = 'free_time' "
            "AND status IN ('pending', 'running', 'ready') "
            "AND run_key NOT LIKE ?",
            (now_iso, _day_prefix(local_date)),
        )
        conn.execute(
            "DELETE FROM heartbeat_schedules WHERE entry_kind = 'free_time'"
        )
        plan = conn.execute(
            "SELECT wake_reason FROM heartbeat_schedules "
            "WHERE entry_kind = 'free_time_plan'"
        ).fetchone()
        if plan is not None and plan["wake_reason"] == marker:
            conn.commit()
            return created

        conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', "
            "outcome = 'plan_replaced', handled_at = ?, next_attempt_at = NULL "
            "WHERE entry_kind = 'free_time' AND run_key LIKE ? "
            "AND status = 'pending'",
            (now_iso, _day_prefix(local_date)),
        )
        conn.execute(
            "DELETE FROM heartbeat_schedules WHERE entry_kind = 'free_time_plan'"
        )

        consumed = conn.execute(
            "SELECT COUNT(*) FROM heartbeat_runs "
            "WHERE entry_kind = 'free_time' AND run_key LIKE ? "
            "AND attempt_count > 0",
            (_day_prefix(local_date),),
        ).fetchone()[0]
        remaining = max(0, max_daily - int(consumed))
        planned: list[tuple[str, datetime]] = []
        if remaining and local_now < end:
            window_seconds = (end - start).total_seconds()
            for ordinal in range(remaining):
                if rng.random() >= FREE_TIME_ACTIVATION_CHANCE:
                    continue
                due = start + timedelta(seconds=rng.random() * window_seconds)
                if due <= local_now:
                    continue
                due_utc = due.astimezone(UTC)
                run_key = f"free_time:{local_date}:{ordinal}:{due_utc.isoformat()}"
                planned.append((run_key, due_utc))
            planned.sort(key=lambda item: item[1])

        search_keys = _select_search_run_keys(
            [run_key for run_key, _due in planned],
            rng,
        )
        for run_key, due in planned:
            conn.execute(
                "INSERT OR IGNORE INTO heartbeat_runs "
                "(run_key, entry_kind, user_id, channel_id, transport, "
                "wake_reason, facts_json, status, next_attempt_at, created_at) "
                "VALUES (?, 'free_time', ?, ?, ?, 'daily_random', ?, "
                "'pending', ?, ?)",
                (
                    run_key,
                    user_id,
                    channel_id,
                    transport,
                    json.dumps(
                        {"direct_search": run_key in search_keys},
                        separators=(",", ":"),
                    ),
                    _iso(due),
                    now_iso,
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                created.append(run_key)

        conn.execute(
            "INSERT INTO heartbeat_schedules "
            "(entry_kind, next_due_at, wake_reason, updated_at) "
            "VALUES ('free_time_plan', ?, ?, ?)",
            (_iso(end.astimezone(UTC)), marker, now_iso),
        )
        conn.commit()
        return created
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def expire_unusable_free_time_runs(
    *,
    now: datetime,
    active_chat: bool,
    awake: bool,
) -> int:
    """Expire missed, sleeping, or chat-conflicting opportunities."""
    now_iso = _iso(now)
    cutoff = _iso(now.astimezone(UTC) - FREE_TIME_MISSED_GRACE)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        due_before = now_iso if active_chat or not awake else cutoff
        outcome = "active_chat" if active_chat else "asleep" if not awake else "expired"
        expired = conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', outcome = ?, "
            "handled_at = ?, next_attempt_at = NULL, claim_token = NULL, "
            "lease_until = NULL, last_error = '' "
            "WHERE entry_kind = 'free_time' AND status = 'pending' "
            "AND next_attempt_at <= ?",
            (outcome, now_iso, due_before),
        ).rowcount
        interrupted = conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', "
            "outcome = 'interrupted', handled_at = ?, next_attempt_at = NULL, "
            "claim_token = NULL, lease_until = NULL, last_error = '' "
            "WHERE entry_kind = 'free_time' AND status IN ('running', 'ready') "
            "AND (lease_until IS NULL OR lease_until <= ?)",
            (now_iso, now_iso),
        ).rowcount
        conn.commit()
        return expired + interrupted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_schedulable_runs(*, now: datetime) -> list[dict]:
    now_iso = _iso(now)
    cutoff = _iso(now.astimezone(UTC) - FREE_TIME_MISSED_GRACE)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM heartbeat_runs WHERE entry_kind = 'free_time' "
            "AND status = 'pending' AND next_attempt_at > ? "
            "AND next_attempt_at <= ? "
            "ORDER BY created_at, run_key",
            (cutoff, now_iso),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def claim_run(
    run_key: str, *, now: datetime, lease_seconds: int = _LEASE_SECONDS,
) -> dict | None:
    now = now.astimezone(UTC)
    now_iso = _iso(now)
    claim_token = f"{now_iso}:{uuid.uuid4().hex}"
    lease_until = _iso(now + timedelta(seconds=lease_seconds))
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM heartbeat_runs WHERE run_key = ?", (run_key,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        item = dict(row)
        status = item["status"]
        lease = _as_utc(item.get("lease_until"))
        if status != "pending":
            conn.rollback()
            return None
        if item["entry_kind"] != "free_time":
            conn.rollback()
            return None
        if lease and lease > now:
            conn.rollback()
            return None
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = 'running', claim_token = ?, "
            "lease_until = ?, attempt_count = attempt_count + 1, "
            "delivery_started_at = NULL WHERE run_key = ? AND status = ?",
            (claim_token, lease_until, run_key, status),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        item.update(
            status="running",
            claim_token=claim_token,
            lease_until=lease_until,
            attempt_count=int(item.get("attempt_count") or 0) + 1,
        )
        return item
    finally:
        conn.close()


def entry_from_claim(claimed: dict) -> MainRuntimeEntry:
    try:
        payload = json.loads(claimed.get("facts_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    common = {
        "run_key": claimed["run_key"],
        "wake_reason": claimed["wake_reason"],
        "user_id": claimed["user_id"],
        "channel_id": claimed["channel_id"],
        "transport": claimed["transport"],
        "claim_token": claimed["claim_token"],
        "lease_until": claimed["lease_until"],
    }
    if claimed["entry_kind"] != "free_time":
        raise ValueError("only Free Time heartbeat runs can enter Main")
    return MainRuntimeEntry.free_time(
        direct_search=bool(payload.get("direct_search")),
        chat_generation=int(claimed.get("_chat_activity_generation") or 0),
        **common,
    )


def store_prepared_result(claimed: dict, durable: DurableChatResult) -> bool:
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = 'ready', result_json = ?, "
            "outcome = 'ready', last_error = '' WHERE run_key = ? "
            "AND status = 'running' AND claim_token = ?",
            (durable.to_json(), claimed["run_key"], claimed["claim_token"]),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def complete_without_delivery(
    claimed: dict, durable: DurableChatResult, outcome: str,
) -> bool:
    if outcome not in {
        "skip",
        "no_effect",
        "tools_only",
        "suppressed",
        "active_chat",
        "stale",
        "delivery_failed",
        "delivery_unknown",
    }:
        raise ValueError("invalid autonomous Main outcome")
    now_iso = _iso(_utc_now())
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', result_json = ?, "
            "outcome = ?, handled_at = ?, claim_token = NULL, lease_until = NULL, "
            "next_attempt_at = NULL, last_error = '' WHERE run_key = ? "
            "AND status IN ('running', 'ready') AND claim_token = ?",
            (
                durable.to_json(), outcome, now_iso,
                claimed["run_key"], claimed["claim_token"],
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def recover_prior_tool_attempt(claimed: dict) -> DurableChatResult | None:
    executions = get_tool_executions_for_turn(claimed["run_key"])
    if not executions:
        return None
    return DurableChatResult(
        tool_audit=tuple(
            {
                "name": item["tool_name"],
                "status": item["status"],
                "state_changed": bool(item["state_changed"]),
            }
            for item in executions
        ),
        successful_effects=any(
            item["status"] == "success" and item["state_changed"]
            for item in executions
        ),
        disposition="handled",
    )


def begin_delivery(
    claimed: dict,
    *,
    now: datetime | None = None,
) -> bool:
    now_iso = _iso(now or _utc_now())
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET delivery_started_at = ? "
            "WHERE run_key = ? AND status = 'ready' AND claim_token = ? "
            "AND delivery_started_at IS NULL",
            (
                now_iso,
                claimed["run_key"],
                claimed["claim_token"],
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()
def store_delivery_progress(claimed: dict, remaining: DurableChatResult) -> bool:
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET result_json = ? WHERE run_key = ? "
            "AND status = 'ready' AND claim_token = ? "
            "AND delivery_started_at IS NOT NULL",
            (
                remaining.to_json(),
                claimed["run_key"],
                claimed["claim_token"],
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def checkpoint_text_delivery(
    claimed: dict, *, content: str, entry_kind: str,
) -> bool:
    now_iso = _iso(_utc_now())
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET text_delivered_at = ? WHERE run_key = ? "
            "AND status = 'ready' AND claim_token = ? "
            "AND text_delivered_at IS NULL",
            (now_iso, claimed["run_key"], claimed["claim_token"]),
        )
        if cursor.rowcount == 1:
            conn.execute(
                "INSERT INTO proactive_log (type, content, created_at) "
                "VALUES (?, ?, ?)",
                (entry_kind, content, now_iso),
            )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def checkpoint_visible_delivery(claimed: dict) -> bool:
    """Count a sticker-only delivery without creating text history or logs."""
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET text_delivered_at = "
            "COALESCE(text_delivered_at, ?) WHERE run_key = ? "
            "AND status = 'ready' AND claim_token = ? "
            "AND delivery_started_at IS NOT NULL",
            (_iso(_utc_now()), claimed["run_key"], claimed["claim_token"]),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def record_failure(claimed: dict, error: str) -> datetime | None:
    now = _utc_now()
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', outcome = 'failed', "
            "handled_at = ?, next_attempt_at = NULL, last_error = ?, "
            "claim_token = NULL, lease_until = NULL, delivery_started_at = NULL "
            "WHERE run_key = ? AND claim_token = ? "
            "AND status IN ('running', 'ready')",
            (_iso(now), error[:1000], claimed["run_key"], claimed["claim_token"]),
        )
        conn.commit()
        return now if cursor.rowcount else None
    finally:
        conn.close()


def complete_delivery(claimed: dict) -> bool:
    now_iso = _iso(_utc_now())
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', outcome = 'delivered', "
            "handled_at = ?, claim_token = NULL, lease_until = NULL, "
            "next_attempt_at = NULL, last_error = '', delivery_started_at = NULL "
            "WHERE run_key = ? AND status = 'ready' AND claim_token = ?",
            (now_iso, claimed["run_key"], claimed["claim_token"]),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def remove_delivered_component(
    durable: DurableChatResult, kind: str, value: str,
) -> DurableChatResult:
    if kind == "text":
        return replace(durable, text="")
    stickers = list(durable.stickers)
    stickers.remove(value)
    return replace(durable, stickers=tuple(stickers))
