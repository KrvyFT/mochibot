"""Reminder persistence and durable delivery state transitions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mochi.config import TZ
from mochi.db import _connect


ACTIVE_STATUSES = ("pending", "running", "ready")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _effective_at(reminder: dict, now: datetime) -> datetime | None:
    remind_at = _as_utc(reminder.get("remind_at"))
    retry_at = _as_utc(reminder.get("next_attempt_at"))
    status = reminder.get("status")
    if status in {"running", "ready"}:
        lease_until = _as_utc(reminder.get("lease_until"))
        if lease_until and lease_until > now:
            return None
    candidates = [value for value in (remind_at, retry_at) if value is not None]
    return max(candidates) if candidates else None


def create_reminder(
    user_id: int,
    channel_id: int,
    message: str,
    remind_at: str,
) -> int:
    conn = _connect()
    try:
        cursor = conn.execute(
            "INSERT INTO reminders "
            "(user_id, channel_id, message, remind_at, kind, status, fired) "
            "VALUES (?, ?, ?, ?, 'notify', 'pending', 0)",
            (user_id, channel_id, message, remind_at),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def create_self_reminder(
    user_id: int,
    channel_id: int,
    intent: str,
    remind_at: str,
    transport: str,
) -> int:
    """Persist a one-time private intent for future Main."""
    conn = _connect()
    try:
        cursor = conn.execute(
            "INSERT INTO reminders "
            "(user_id, channel_id, message, remind_at, recurrence, kind, "
            "context, source, transport, status, fired) "
            "VALUES (?, ?, '', ?, NULL, 'self', ?, 'main', ?, 'pending', 0)",
            (user_id, channel_id, remind_at, intent, transport or None),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def get_active_reminders(user_id: int) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, user_id, channel_id, message, remind_at, kind, "
            "context, source, status FROM reminders "
            "WHERE user_id = ? AND kind IN ('notify', 'self') "
            "AND status IN ('pending', 'running', 'ready') "
            "ORDER BY remind_at ASC",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_pending_reminders(user_id: int | None = None) -> list[dict]:
    """Compatibility query for active ordinary notifications."""
    conn = _connect()
    try:
        params: tuple[int, ...] = ()
        user_clause = ""
        if user_id is not None:
            user_clause = "AND user_id = ? "
            params = (user_id,)
        rows = conn.execute(
            "SELECT id, user_id, channel_id, message, remind_at, status "
            "FROM reminders WHERE kind = 'notify' "
            "AND status IN ('pending', 'running', 'ready') "
            f"{user_clause}ORDER BY remind_at ASC",
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def update_active_reminder(
    reminder_id: int,
    user_id: int,
    *,
    remind_at: str | None = None,
    content: str | None = None,
) -> bool:
    """Update one pending owner reminder without racing claimed delivery."""
    assignments = []
    params: list[object] = []
    if remind_at is not None:
        assignments.append("remind_at = ?")
        params.append(remind_at)
    if content is not None:
        assignments.append(
            "message = CASE WHEN kind = 'notify' THEN ? ELSE message END"
        )
        params.append(content)
        assignments.append(
            "context = CASE WHEN kind = 'self' THEN ? ELSE context END"
        )
        params.append(content)
    if not assignments:
        return False
    assignments.extend([
        "next_attempt_at = NULL",
        "last_error = NULL",
        "attempt_count = 0",
    ])
    params.extend([reminder_id, user_id])
    conn = _connect()
    try:
        cursor = conn.execute(
            f"UPDATE reminders SET {', '.join(assignments)} "
            "WHERE id = ? AND user_id = ? "
            "AND kind IN ('notify', 'self') AND status = 'pending'",
            params,
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def cancel_reminder(reminder_id: int, user_id: int | None = None) -> bool:
    """Soft-cancel active work, scoped to the owner when supplied."""
    now_iso = _iso(_now())
    conn = _connect()
    try:
        owner_clause = "AND user_id = ? " if user_id is not None else ""
        params = [now_iso, reminder_id]
        if user_id is not None:
            params.append(user_id)
        params.append(now_iso)
        cursor = conn.execute(
            "UPDATE reminders SET status = 'cancelled', cancelled_at = ?, "
            "claimed_at = NULL, lease_until = NULL, next_attempt_at = NULL "
            "WHERE id = ? "
            f"{owner_clause}"
            "AND kind IN ('notify', 'self') "
            "AND status IN ('pending', 'running', 'ready') "
            "AND (lease_until IS NULL OR lease_until <= ?)",
            params,
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_schedulable_reminders(
    now: datetime | None = None,
) -> list[dict]:
    """Return active reminders ordered by next durable wake-up time."""
    now = (now or _now()).astimezone(timezone.utc)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, user_id, channel_id, message, remind_at, recurrence, "
            "kind, context, source, transport, status, claimed_at, lease_until, "
            "attempt_count, next_attempt_at, last_error, prepared_text, "
            "result_json, outcome, handled_at, delivery_cursor, "
            "delivery_started_at FROM reminders "
            "WHERE kind IN ('notify', 'self') "
            "AND status IN ('pending', 'running', 'ready')"
        ).fetchall()
    finally:
        conn.close()
    result = []
    for row in rows:
        reminder = dict(row)
        effective_at = _effective_at(reminder, now)
        if effective_at is None:
            continue
        reminder["effective_at"] = _iso(effective_at)
        result.append(reminder)
    result.sort(key=lambda item: (item["effective_at"], item["id"]))
    return result


def get_next_active_lease_expiry(
    now: datetime | None = None,
) -> datetime | None:
    now = (now or _now()).astimezone(timezone.utc)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT lease_until FROM reminders "
            "WHERE kind IN ('notify', 'self') "
            "AND status IN ('running', 'ready') AND lease_until IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    expiries = [
        expiry for row in rows
        if (expiry := _as_utc(row["lease_until"])) is not None
        and expiry > now
    ]
    return min(expiries) if expiries else None


def claim_reminder(
    reminder_id: int,
    *,
    now: datetime | None = None,
    lease_seconds: int = 300,
) -> dict | None:
    """Atomically lease due work or recover an expired claim."""
    now = (now or _now()).astimezone(timezone.utc)
    now_iso = _iso(now)
    lease_until = _iso(now + timedelta(seconds=lease_seconds))
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ? "
            "AND kind IN ('notify', 'self')",
            (reminder_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        reminder = dict(row)
        status = reminder.get("status")
        if status not in ACTIVE_STATUSES:
            conn.rollback()
            return None
        effective_at = _effective_at(reminder, now)
        if effective_at is None or effective_at > now:
            conn.rollback()
            return None
        claimed_status = (
            "ready"
            if reminder.get("prepared_text") or reminder.get("result_json")
            else "running"
        )
        cursor = conn.execute(
            "UPDATE reminders SET status = ?, claimed_at = ?, lease_until = ?, "
            "delivery_started_at = NULL WHERE id = ? AND status = ?",
            (
                claimed_status, now_iso, lease_until,
                reminder_id, status,
            ),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        reminder.update(
            status=claimed_status,
            claimed_at=now_iso,
            lease_until=lease_until,
        )
        return reminder
    finally:
        conn.close()


def store_prepared_text(
    reminder_id: int,
    claimed_at: str,
    text: str,
) -> bool:
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE reminders SET status = 'ready', prepared_text = ?, "
            "last_error = NULL WHERE id = ? AND status = 'running' "
            "AND claimed_at = ?",
            (text, reminder_id, claimed_at),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def store_prepared_result(
    reminder_id: int,
    claimed_at: str,
    result_json: str,
) -> bool:
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE reminders SET status = 'ready', result_json = ?, "
            "outcome = 'ready', last_error = NULL "
            "WHERE id = ? AND kind = 'self' AND status = 'running' "
            "AND claimed_at = ?",
            (result_json, reminder_id, claimed_at),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def complete_without_delivery(
    reminder_id: int,
    claimed_at: str,
    result_json: str,
    outcome: str,
    *,
    handled_at: datetime | None = None,
) -> bool:
    if outcome not in {"no_op", "handled"}:
        raise ValueError("invalid reminder terminal outcome")
    handled_at = (handled_at or _now()).astimezone(timezone.utc)
    handled_iso = _iso(handled_at)
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE reminders SET status = 'delivered', fired = 1, "
            "result_json = ?, outcome = ?, handled_at = ?, delivered_at = ?, "
            "claimed_at = NULL, lease_until = NULL, next_attempt_at = NULL, "
            "last_error = NULL WHERE id = ? AND kind = 'self' "
            "AND status = 'running' AND claimed_at = ?",
            (
                result_json, outcome, handled_iso, handled_iso,
                reminder_id, claimed_at,
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def begin_delivery(reminder_id: int, claimed_at: str) -> int | None:
    """Advance the best-effort cursor before crossing the transport boundary."""
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE reminders SET delivery_cursor = delivery_cursor + 1, "
            "delivery_started_at = ? "
            "WHERE id = ? AND status = 'ready' AND claimed_at = ? "
            "AND delivery_started_at IS NULL",
            (_iso(_now()), reminder_id, claimed_at),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        row = conn.execute(
            "SELECT delivery_cursor FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
        conn.commit()
        return int(row["delivery_cursor"])
    finally:
        conn.close()


def store_delivery_progress(
    reminder_id: int,
    claimed_at: str,
    result_json: str,
) -> bool:
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE reminders SET result_json = ? "
            "WHERE id = ? AND kind = 'self' AND status = 'ready' "
            "AND claimed_at = ? AND delivery_started_at IS NOT NULL",
            (result_json, reminder_id, claimed_at),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def record_reminder_failure(
    reminder_id: int,
    claimed_at: str,
    error: str,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Release a lease with bounded exponential backoff."""
    now = (now or _now()).astimezone(timezone.utc)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempt_count, prepared_text, result_json FROM reminders "
            "WHERE id = ? AND claimed_at = ? "
            "AND status IN ('running', 'ready')",
            (reminder_id, claimed_at),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        attempt_count = int(row["attempt_count"] or 0) + 1
        delay = min(60 * (2 ** min(attempt_count - 1, 6)), 3600)
        retry_at = now + timedelta(seconds=delay)
        retry_status = (
            "ready" if row["prepared_text"] or row["result_json"] else "pending"
        )
        conn.execute(
            "UPDATE reminders SET status = ?, attempt_count = ?, "
            "next_attempt_at = ?, last_error = ?, claimed_at = NULL, "
            "lease_until = NULL, delivery_started_at = NULL "
            "WHERE id = ? AND claimed_at = ?",
            (
                retry_status, attempt_count, _iso(retry_at), error[:1000],
                reminder_id, claimed_at,
            ),
        )
        conn.commit()
        return retry_at
    finally:
        conn.close()


def complete_reminder_delivery(
    reminder_id: int,
    claimed_at: str,
    *,
    delivered_at: datetime | None = None,
    next_remind_at: str | None = None,
) -> bool:
    delivered_at = (delivered_at or _now()).astimezone(timezone.utc)
    conn = _connect()
    try:
        if next_remind_at:
            cursor = conn.execute(
                "UPDATE reminders SET status = 'pending', fired = 0, "
                "remind_at = ?, claimed_at = NULL, lease_until = NULL, "
                "attempt_count = 0, next_attempt_at = NULL, last_error = NULL, "
                "prepared_text = NULL, result_json = NULL, outcome = NULL, "
                "delivery_cursor = 0, delivery_started_at = NULL, "
                "delivered_at = NULL WHERE id = ? AND status = 'ready' "
                "AND claimed_at = ?",
                (next_remind_at, reminder_id, claimed_at),
            )
        else:
            delivered_iso = _iso(delivered_at)
            cursor = conn.execute(
                "UPDATE reminders SET status = 'delivered', fired = 1, "
                "delivered_at = ?, handled_at = ?, outcome = 'delivered', "
                "claimed_at = NULL, lease_until = NULL, next_attempt_at = NULL, "
                "last_error = NULL, delivery_started_at = NULL "
                "WHERE id = ? AND status = 'ready' AND claimed_at = ?",
                (
                    delivered_iso, delivered_iso,
                    reminder_id, claimed_at,
                ),
            )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def mark_reminder_fired(reminder_id: int) -> None:
    now_iso = _iso(_now())
    conn = _connect()
    try:
        conn.execute(
            "UPDATE reminders SET status = 'delivered', fired = 1, "
            "delivered_at = ?, claimed_at = NULL, lease_until = NULL "
            "WHERE id = ?",
            (now_iso, reminder_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_upcoming_reminders(user_id: int, hours_ahead: int = 2) -> list[dict]:
    now = datetime.now(TZ)
    cutoff = (now + timedelta(hours=hours_ahead)).isoformat()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, message, remind_at FROM reminders "
            "WHERE user_id = ? AND kind = 'notify' "
            "AND status IN ('pending', 'running', 'ready') "
            "AND remind_at <= ? ORDER BY remind_at",
            (user_id, cutoff),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_reminder_diagnostic_section() -> str:
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT id, user_id, kind, message, context, remind_at, "
            "recurrence, status, attempt_count, next_attempt_at, last_error "
            "FROM reminders WHERE kind IN ('notify', 'self') "
            "AND status IN ('pending', 'running', 'ready') "
            "ORDER BY remind_at"
        ).fetchall()
        conn.close()
        lines = ["--- Reminder State ---", f"Active: {len(rows)}"]
        for row in rows:
            reminder = dict(row)
            content = (
                reminder.get("context") or ""
                if reminder["kind"] == "self"
                else reminder["message"]
            )
            lines.append(
                f"  #{reminder['id']} kind={reminder['kind']} "
                f"status={reminder['status']} remind_at={reminder['remind_at']} "
                f"content={content[:50]}"
            )
        if not rows:
            lines.append("  (none)")
        return "\n".join(lines)
    except Exception as exc:
        return f"--- Reminder State ---\n(query failed: {exc})"
