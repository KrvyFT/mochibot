"""Habit skill — DB queries.

Canonical source for habit CRUD and check-in logic.
Other modules should import from here.
"""

from datetime import datetime, timedelta

from mochi.db import _connect
from mochi.config import TZ


def add_habit(user_id: int, name: str, frequency: str,
              category: str = "", importance: str = "normal",
              context: str = "") -> tuple[int, bool]:
    """Create or reactivate a habit, preserving its id and history.

    frequency: "daily:N" (N times/day) or "weekly:N" (N times/week)
               or "weekly_on:DAY,...:N".
    importance: "important" or "normal".
    context: descriptive note (e.g. "morning and evening, after meals").
    Returns ``(habit_id, reactivated)``.
    """
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id, active FROM habits WHERE user_id = ? AND name = ?",
            (user_id, name),
        ).fetchone()
        if existing is not None:
            if existing["active"]:
                raise ValueError(f"habit '{name}' already exists")
            conn.execute(
                "UPDATE habits SET frequency = ?, category = ?, importance = ?, "
                "context = ?, active = 1, paused_until = NULL, "
                "snoozed_until = NULL WHERE id = ?",
                (
                    frequency,
                    category,
                    importance,
                    context,
                    existing["id"],
                ),
            )
            conn.commit()
            return int(existing["id"]), True

        cursor = conn.execute(
            "INSERT INTO habits (user_id, name, frequency, category, "
            "importance, context, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, frequency, category, importance, context, now),
        )
        habit_id = int(cursor.lastrowid)
        conn.commit()
        return habit_id, False
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_habits(user_id: int, active_only: bool = True) -> list[dict]:
    """Return habits for a user."""
    conn = _connect()
    if active_only:
        rows = conn.execute(
            "SELECT * FROM habits WHERE user_id = ? AND active = 1 ORDER BY id",
            (user_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM habits WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def deactivate_habit(user_id: int, habit_id: int) -> bool:
    """Deactivate (soft-delete) a habit. Returns True if updated."""
    conn = _connect()
    cursor = conn.execute(
        "UPDATE habits SET active = 0 WHERE id = ? AND user_id = ?",
        (habit_id, user_id),
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def update_habit(user_id: int, habit_id: int, **fields) -> bool:
    """Update mutable fields on a habit. Returns True if updated.

    Allowed fields: name, context, importance, category, frequency.
    """
    allowed = {"name", "context", "importance", "category", "frequency"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [habit_id, user_id]
    conn = _connect()
    cursor = conn.execute(
        f"UPDATE habits SET {set_clause} "
        "WHERE id = ? AND user_id = ? AND active = 1",
        values,
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def add_habit_checkins(
    habit_id: int,
    user_id: int,
    period: str,
    count: int,
    note: str = "",
) -> int:
    """Atomically append check-ins and return the committed period total."""
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ValueError("count must be a positive integer")
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT COUNT(*) AS cnt FROM habit_logs "
            "WHERE habit_id = ? AND user_id = ? AND period = ?",
            (habit_id, user_id, period),
        ).fetchone()["cnt"]
        conn.executemany(
            "INSERT INTO habit_logs "
            "(habit_id, user_id, note, logged_at, period) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (habit_id, user_id, note, now, period)
                for _ in range(count)
            ],
        )
        conn.commit()
        return current + count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reconcile_habit_total(
    habit_id: int,
    user_id: int,
    period: str,
    total: int,
    note: str = "",
) -> tuple[int, int]:
    """Atomically add the missing check-ins needed to reach a reported total."""
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError("total must be a non-negative integer")
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT COUNT(*) AS cnt FROM habit_logs "
            "WHERE habit_id = ? AND user_id = ? AND period = ?",
            (habit_id, user_id, period),
        ).fetchone()["cnt"]
        if total < current:
            raise ValueError(
                f"reported total {total} is below current progress {current}"
            )
        added = total - current
        conn.executemany(
            "INSERT INTO habit_logs "
            "(habit_id, user_id, note, logged_at, period) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (habit_id, user_id, note, now, period)
                for _ in range(added)
            ],
        )
        conn.commit()
        return current, added
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_habit_checkins(habit_id: int, period: str) -> list[dict]:
    """Return check-in logs for a habit in a specific period."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM habit_logs WHERE habit_id = ? AND period = ? "
        "ORDER BY logged_at",
        (habit_id, period),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def undo_latest_habit_checkin(
    habit_id: int,
    user_id: int,
    period: str,
) -> int | None:
    """Atomically delete the latest owner-scoped check-in and return the remainder."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        latest = conn.execute(
            "SELECT id FROM habit_logs "
            "WHERE habit_id = ? AND user_id = ? AND period = ? "
            "ORDER BY logged_at DESC, id DESC LIMIT 1",
            (habit_id, user_id, period),
        ).fetchone()
        if latest is None:
            conn.commit()
            return None
        deleted = conn.execute(
            "DELETE FROM habit_logs "
            "WHERE id = ? AND habit_id = ? AND user_id = ? AND period = ?",
            (latest["id"], habit_id, user_id, period),
        )
        if deleted.rowcount != 1:
            raise RuntimeError("latest habit check-in changed during undo")
        remaining = conn.execute(
            "SELECT COUNT(*) AS cnt FROM habit_logs "
            "WHERE habit_id = ? AND user_id = ? AND period = ?",
            (habit_id, user_id, period),
        ).fetchone()["cnt"]
        conn.commit()
        return remaining
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_habit_stats(habit_id: int, periods: list[str]) -> dict:
    """Return check-in counts keyed by period for a habit.

    periods: list of period strings, e.g. ["2026-02-22", "2026-02-21"].
    Returns {period: count}.
    """
    if not periods:
        return {}
    conn = _connect()
    placeholders = ",".join("?" for _ in periods)
    rows = conn.execute(
        f"SELECT period, COUNT(*) as cnt FROM habit_logs "
        f"WHERE habit_id = ? AND period IN ({placeholders}) "
        f"GROUP BY period",
        [habit_id] + periods,
    ).fetchall()
    conn.close()
    return {r["period"]: r["cnt"] for r in rows}


def get_habit_streak(
    habit_id: int, cycle: str, target: int,
    allowed_days: set[int] | None = None, max_lookback: int = 90,
) -> int:
    """Compute current streak (consecutive completed periods) for a habit.

    For daily habits: walks backwards from yesterday, skipping non-allowed days.
    For weekly habits: walks backwards from last week.
    Returns 0 if the most recent eligible period was missed.
    """
    now = datetime.now(TZ)
    if cycle == "daily":
        # logical 起点：roll back if before MAINTENANCE_HOUR
        from mochi.admin.admin_db import get_system_config
        logical_now = now - timedelta(days=1) if now.hour < get_system_config("MAINTENANCE_HOUR") else now
        periods = []
        for i in range(1, max_lookback + 1):
            d = logical_now - timedelta(days=i)
            if allowed_days is not None and d.weekday() not in allowed_days:
                continue
            periods.append(d.strftime("%Y-%m-%d"))
    else:
        # wall-clock 故意：ISO 周边界在 Mon 00:00，与 maintenance window (0-3) 不冲突
        periods = []
        for i in range(1, max_lookback // 7 + 1):
            d = now - timedelta(weeks=i)
            periods.append(d.strftime("%G-W%V"))

    if not periods:
        return 0

    stats = get_habit_stats(habit_id, periods)
    streak = 0
    for p in periods:
        if stats.get(p, 0) >= target:
            streak += 1
        else:
            break
    return streak


def pause_habit(user_id: int, habit_id: int, until_date: str) -> bool:
    """Pause a habit until the given ISO date (inclusive). Returns True if updated."""
    conn = _connect()
    cur = conn.execute(
        "UPDATE habits SET paused_until = ? WHERE id = ? AND user_id = ? AND active = 1",
        (until_date, habit_id, user_id),
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def resume_habit(user_id: int, habit_id: int) -> bool:
    """Resume a paused habit (clear paused_until). Returns True if updated."""
    conn = _connect()
    cur = conn.execute(
        "UPDATE habits SET paused_until = NULL WHERE id = ? AND user_id = ? AND active = 1",
        (habit_id, user_id),
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok
