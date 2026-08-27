"""Durable one-shot scheduling and delivery state for Free Time."""

from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, time, timedelta, timezone

from mochi.config import TZ
from mochi.db import _connect, get_tool_executions_for_turn
from mochi.free_time import (
    card_from_run_payload,
    search_available_from_run_payload,
)
from mochi.main_runtime import DurableChatResult, MainRuntimeEntry


UTC = timezone.utc
FREE_TIME_AWAKE_START = time(6, 0)
FREE_TIME_AWAKE_END = time(21, 0)
FREE_TIME_ACTIVATION_CHANCE = 0.5
FREE_TIME_MISSED_GRACE = timedelta(minutes=10)
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


def ensure_daily_free_time_plan(
    *,
    user_id: int,
    channel_id: int,
    transport: str,
    now: datetime,
    max_daily: int,
    rng: random.Random | random.SystemRandom | None = None,
) -> list[str]:
    """Create today's bounded random opportunities once, without calling Main."""
    rng = rng or random.SystemRandom()
    local_now = now.astimezone(TZ)
    local_date = local_now.date().isoformat()
    now_utc = local_now.astimezone(UTC)
    now_iso = _iso(now_utc)
    max_daily = max(0, min(50, int(max_daily)))
    start = datetime.combine(
        local_now.date(), FREE_TIME_AWAKE_START, tzinfo=TZ,
    )
    end = datetime.combine(
        local_now.date(), FREE_TIME_AWAKE_END, tzinfo=TZ,
    )
    marker = f"{local_date}"
    created: list[str] = []

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', outcome = 'expired', "
            "handled_at = ?, claim_token = NULL, lease_until = NULL, "
            "next_attempt_at = NULL, last_error = '' "
            "WHERE entry_kind = 'free_time' AND status IN ('pending', 'running', 'ready') "
            "AND run_key NOT LIKE ?",
            (now_iso, _day_prefix(local_date)),
        )
        plan = conn.execute(
            "SELECT wake_reason FROM heartbeat_schedules "
            "WHERE entry_kind = 'free_time_plan'"
        ).fetchone()

        if plan is None or plan["wake_reason"] != marker:
            conn.execute(
                "DELETE FROM heartbeat_schedules WHERE entry_kind = 'free_time_plan'"
            )
            if max_daily and local_now < end:
                slot_seconds = (end - start).total_seconds() / max_daily
                for ordinal in range(max_daily):
                    if rng.random() >= FREE_TIME_ACTIVATION_CHANCE:
                        continue
                    slot_start = start + timedelta(seconds=slot_seconds * ordinal)
                    slot_end = start + timedelta(seconds=slot_seconds * (ordinal + 1))
                    due = slot_start + timedelta(
                        seconds=rng.random() * (slot_end - slot_start).total_seconds()
                    )
                    if due <= local_now:
                        continue
                    due_utc = due.astimezone(UTC)
                    run_key = (
                        f"free_time:{local_date}:{ordinal}:"
                        f"{due_utc.isoformat()}"
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO heartbeat_runs "
                        "(run_key, entry_kind, user_id, channel_id, transport, "
                        "wake_reason, facts_json, status, next_attempt_at, created_at) "
                        "VALUES (?, 'free_time', ?, ?, ?, 'random_slot', '{}', "
                        "'pending', ?, ?)",
                        (
                            run_key,
                            user_id,
                            channel_id,
                            transport,
                            _iso(due_utc),
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

        _apply_daily_cap(
            conn,
            local_date=local_date,
            max_daily=max_daily,
            now_iso=now_iso,
        )
        conn.commit()
        return created
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _apply_daily_cap(
    conn,
    *,
    local_date: str,
    max_daily: int,
    now_iso: str,
) -> None:
    rows = conn.execute(
        "SELECT run_key, status FROM heartbeat_runs "
        "WHERE entry_kind = 'free_time' AND run_key LIKE ? "
        "ORDER BY next_attempt_at, run_key",
        (_day_prefix(local_date),),
    ).fetchall()
    consumed = sum(row["status"] != "pending" for row in rows)
    pending = [row for row in rows if row["status"] == "pending"]
    keep = max(0, max_daily - consumed)
    for row in pending[keep:]:
        conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', outcome = 'quota_reduced', "
            "handled_at = ?, next_attempt_at = NULL WHERE run_key = ? "
            "AND status = 'pending'",
            (now_iso, row["run_key"]),
        )


def expire_unusable_free_time_runs(
    *,
    now: datetime,
    active_chat: bool = False,
    awake: bool = True,
) -> int:
    """End missed or currently unsuitable opportunities without invoking Main."""
    now_utc = now.astimezone(UTC)
    cutoff = _iso(now_utc - FREE_TIME_MISSED_GRACE)
    now_iso = _iso(now_utc)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', outcome = CASE "
            "WHEN ? THEN 'active_chat' WHEN ? = 0 THEN 'asleep' ELSE 'expired' END, "
            "handled_at = ?, next_attempt_at = NULL, claim_token = NULL, "
            "lease_until = NULL, last_error = '' "
            "WHERE entry_kind = 'free_time' AND status = 'pending' "
            "AND next_attempt_at <= ?",
            (int(active_chat), int(awake), now_iso, now_iso if active_chat or not awake else cutoff),
        )
        stale = conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', outcome = 'interrupted', "
            "handled_at = ?, next_attempt_at = NULL, claim_token = NULL, "
            "lease_until = NULL, last_error = '' "
            "WHERE entry_kind = 'free_time' AND status IN ('running', 'ready') "
            "AND (lease_until IS NULL OR lease_until <= ?)",
            (now_iso, now_iso),
        ).rowcount
        conn.commit()
        return cursor.rowcount + stale
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
            "AND status = 'pending' AND next_attempt_at > ? AND next_attempt_at <= ? "
            "ORDER BY next_attempt_at, run_key",
            (cutoff, now_iso),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def claim_run(
    run_key: str,
    *,
    now: datetime,
    lease_seconds: int = _LEASE_SECONDS,
) -> dict | None:
    now_utc = now.astimezone(UTC)
    now_iso = _iso(now_utc)
    claim_token = f"{now_iso}:{uuid.uuid4().hex}"
    lease_until = _iso(now_utc + timedelta(seconds=lease_seconds))
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM heartbeat_runs WHERE run_key = ? "
            "AND entry_kind = 'free_time' AND status = 'pending' "
            "AND next_attempt_at <= ?",
            (run_key, now_iso),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = 'running', claim_token = ?, "
            "lease_until = ?, next_attempt_at = NULL, delivery_started_at = NULL "
            "WHERE run_key = ? AND status = 'pending'",
            (claim_token, lease_until, run_key),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        item = dict(row)
        item.update(
            status="running",
            claim_token=claim_token,
            lease_until=lease_until,
            next_attempt_at=None,
        )
        return item
    finally:
        conn.close()


def entry_from_claim(claimed: dict) -> MainRuntimeEntry:
    if claimed["entry_kind"] != "free_time":
        raise ValueError("Only Free Time enters the generic autonomous runtime")
    return MainRuntimeEntry.free_time(
        run_key=claimed["run_key"],
        wake_reason=claimed["wake_reason"],
        user_id=claimed["user_id"],
        channel_id=claimed["channel_id"],
        transport=claimed["transport"],
        claim_token=claimed["claim_token"],
        lease_until=claimed["lease_until"],
        card=card_from_run_payload(claimed.get("facts_json")),
        search_available=search_available_from_run_payload(
            claimed.get("facts_json")
        ),
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
    claimed: dict,
    durable: DurableChatResult,
    outcome: str,
) -> bool:
    allowed = {
        "no_effect",
        "tools_only",
        "active_chat",
        "expired",
        "asleep",
        "timeout",
        "failure",
        "delivery_failed",
        "delivery_unknown",
        "interrupted",
    }
    if outcome not in allowed:
        raise ValueError("invalid Free Time outcome")
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', result_json = ?, "
            "outcome = ?, handled_at = ?, claim_token = NULL, lease_until = NULL, "
            "next_attempt_at = NULL, last_error = '' WHERE run_key = ? "
            "AND status IN ('running', 'ready') AND claim_token = ?",
            (
                durable.to_json(),
                outcome,
                _iso(_utc_now()),
                claimed["run_key"],
                claimed["claim_token"],
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
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET delivery_started_at = ? "
            "WHERE run_key = ? AND status = 'ready' AND claim_token = ? "
            "AND delivery_started_at IS NULL",
            (
                _iso(now or _utc_now()),
                claimed["run_key"],
                claimed["claim_token"],
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def checkpoint_delivery(
    claimed: dict,
    *,
    content: str,
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
                "VALUES ('free_time', ?, ?)",
                (content, now_iso),
            )
        conn.commit()
        return cursor.rowcount == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_delivery(claimed: dict) -> bool:
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', outcome = 'delivered', "
            "handled_at = ?, claim_token = NULL, lease_until = NULL, "
            "next_attempt_at = NULL, last_error = '', delivery_started_at = NULL "
            "WHERE run_key = ? AND status = 'ready' AND claim_token = ?",
            (
                _iso(_utc_now()),
                claimed["run_key"],
                claimed["claim_token"],
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()
