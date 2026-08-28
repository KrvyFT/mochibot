"""Durable scheduling and delivery state for Main heartbeat entries."""

from __future__ import annotations

import random
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from mochi.config import TZ, logical_today
from mochi.db import _connect, get_tool_executions_for_turn
from mochi.main_runtime import DurableChatResult, MainRuntimeEntry


UTC = timezone.utc
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


def ensure_schedules(
    *,
    now: datetime,
    free_time_min_minutes: int,
    free_time_max_minutes: int,
    rng: random.Random | random.SystemRandom | None = None,
) -> None:
    rng = rng or random.SystemRandom()
    now = now.astimezone(UTC)
    free_delay = rng.randint(
        min(free_time_min_minutes, free_time_max_minutes),
        max(free_time_min_minutes, free_time_max_minutes),
    )
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO heartbeat_schedules "
            "(entry_kind, next_due_at, wake_reason, updated_at) "
            "VALUES ('free_time', ?, 'periodic', ?)",
            (_iso(now + timedelta(minutes=free_delay)), _iso(now)),
        )
        conn.commit()
    finally:
        conn.close()


def set_schedule_due(
    kind: str, due_at: datetime, *, wake_reason: str = "periodic",
) -> None:
    """Set one clock directly; primarily useful for deterministic tests."""
    if kind != "free_time":
        raise ValueError("only the Free Time schedule is supported")
    now_iso = _iso(_utc_now())
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO heartbeat_schedules "
            "(entry_kind, next_due_at, wake_reason, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(entry_kind) DO UPDATE SET next_due_at = excluded.next_due_at, "
            "wake_reason = excluded.wake_reason, updated_at = excluded.updated_at",
            (kind, _iso(due_at), wake_reason, now_iso),
        )
        conn.commit()
    finally:
        conn.close()


def materialize_due_runs(
    *,
    user_id: int,
    channel_id: int,
    transport: str,
    now: datetime,
    free_time_min_minutes: int,
    free_time_max_minutes: int,
    free_time_not_before: datetime | None = None,
    rng: random.Random | random.SystemRandom | None = None,
) -> list[str]:
    """Snapshot a due Free Time clock and advance it atomically."""
    rng = rng or random.SystemRandom()
    now = now.astimezone(UTC)
    free_time_not_before = (
        free_time_not_before.astimezone(UTC)
        if free_time_not_before is not None
        else None
    )
    now_iso = _iso(now)
    created: list[str] = []
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        due_rows = conn.execute(
            "SELECT entry_kind, next_due_at, wake_reason FROM heartbeat_schedules "
            "WHERE next_due_at <= ? ORDER BY next_due_at, entry_kind",
            (now_iso,),
        ).fetchall()
        for row in due_rows:
            kind = row["entry_kind"]
            if kind != "free_time":
                continue
            if (
                free_time_not_before is not None
                and free_time_not_before > now
            ):
                conn.execute(
                    "UPDATE heartbeat_schedules SET next_due_at = ?, "
                    "updated_at = ? WHERE entry_kind = 'free_time'",
                    (_iso(free_time_not_before), now_iso),
                )
                continue
            due_at = row["next_due_at"]
            run_key = f"{kind}:{due_at}"
            conn.execute(
                "INSERT OR IGNORE INTO heartbeat_runs "
                "(run_key, entry_kind, user_id, channel_id, transport, wake_reason, "
                "facts_json, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, '[]', 'pending', ?)",
                (
                    run_key, kind, user_id, channel_id, transport,
                    row["wake_reason"], now_iso,
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                created.append(run_key)
            delay = rng.randint(
                min(free_time_min_minutes, free_time_max_minutes),
                max(free_time_min_minutes, free_time_max_minutes),
            )
            next_due = now + timedelta(minutes=delay)
            conn.execute(
                "UPDATE heartbeat_schedules SET next_due_at = ?, "
                "wake_reason = 'periodic', updated_at = ? WHERE entry_kind = ?",
                (_iso(next_due), now_iso, kind),
            )
        conn.commit()
    finally:
        conn.close()
    return created


def get_schedulable_runs(*, now: datetime) -> list[dict]:
    now_iso = _iso(now)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM heartbeat_runs WHERE "
            "entry_kind = 'free_time' AND ("
            "(status = 'ready' AND last_error = 'delivery budget/cooldown' AND "
            "(lease_until IS NULL OR lease_until <= ?)) OR "
            "(status IN ('pending', 'ready') AND "
            "(next_attempt_at IS NULL OR next_attempt_at <= ?) AND "
            "(lease_until IS NULL OR lease_until <= ?)) OR "
            "(status = 'running' AND lease_until <= ?)) "
            "ORDER BY created_at, run_key",
            (now_iso, now_iso, now_iso, now_iso),
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
        retry_at = _as_utc(item.get("next_attempt_at"))
        lease = _as_utc(item.get("lease_until"))
        legacy_budget_queue = (
            status == "ready"
            and item.get("last_error") == "delivery budget/cooldown"
        )
        if status not in {"pending", "ready", "running"}:
            conn.rollback()
            return None
        if item["entry_kind"] != "free_time":
            conn.rollback()
            return None
        if retry_at and retry_at > now and not legacy_budget_queue:
            conn.rollback()
            return None
        if lease and lease > now:
            conn.rollback()
            return None
        claimed_status = "ready" if item.get("result_json") else "running"
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = ?, claim_token = ?, lease_until = ?, "
            "delivery_started_at = NULL WHERE run_key = ? AND status = ?",
            (claimed_status, claim_token, lease_until, run_key, status),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        item.update(
            status=claimed_status,
            claim_token=claim_token,
            lease_until=lease_until,
        )
        return item
    finally:
        conn.close()


def entry_from_claim(claimed: dict) -> MainRuntimeEntry:
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
    return MainRuntimeEntry.free_time(**common)


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


def delivery_wait(
    *,
    now: datetime,
    max_daily: int,
    cooldown_seconds: int,
) -> tuple[int, str | None]:
    from mochi.admin.admin_db import get_system_config

    local_now = now.astimezone(TZ)
    day = logical_today(local_now)
    start = datetime.strptime(day, "%Y-%m-%d").replace(
        hour=get_system_config("MAINTENANCE_HOUR"), tzinfo=TZ,
    ).astimezone(UTC)
    end = start + timedelta(days=1)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS count, MAX(text_delivered_at) AS latest "
            "FROM heartbeat_runs WHERE text_delivered_at >= ? "
            "AND text_delivered_at < ?",
            (_iso(start), _iso(end)),
        ).fetchone()
    finally:
        conn.close()
    if int(row["count"] or 0) >= max_daily:
        return (
            max(1, int((end - now.astimezone(UTC)).total_seconds())),
            "daily_limit",
        )
    latest = _as_utc(row["latest"])
    if latest:
        remaining = cooldown_seconds - int(
            (now.astimezone(UTC) - latest).total_seconds()
        )
        if remaining > 0:
            return remaining, "cooldown"
    return 0, None


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
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempt_count, result_json FROM heartbeat_runs "
            "WHERE run_key = ? AND claim_token = ? "
            "AND status IN ('running', 'ready')",
            (claimed["run_key"], claimed["claim_token"]),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        attempt = int(row["attempt_count"] or 0) + 1
        retry_at = now + timedelta(seconds=min(60 * (2 ** min(attempt - 1, 6)), 3600))
        status = "ready" if row["result_json"] else "pending"
        conn.execute(
            "UPDATE heartbeat_runs SET status = ?, attempt_count = ?, "
            "next_attempt_at = ?, last_error = ?, claim_token = NULL, "
            "lease_until = NULL, delivery_started_at = NULL "
            "WHERE run_key = ? AND claim_token = ?",
            (
                status, attempt, _iso(retry_at), error[:1000],
                claimed["run_key"], claimed["claim_token"],
            ),
        )
        conn.commit()
        return retry_at
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
