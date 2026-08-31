"""Todo skill — DB queries.

Canonical source for todo CRUD and domain queries.
"""

from datetime import datetime
import unicodedata

from mochi.db import _connect
from mochi.config import TZ


def create_todo(user_id: int, task: str,
                nudge_date: str | None = None) -> int:
    """Add a todo item. Returns the new todo id."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO todos (user_id, task, created_at, nudge_date)"
        " VALUES (?, ?, ?, ?)",
        (user_id, task, now, nudge_date),
    )
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def get_todos(user_id: int, include_done: bool = False) -> list[dict]:
    """Return todos for a user."""
    conn = _connect()
    conditions = ["user_id = ?"]
    params: list = [user_id]
    if not include_done:
        conditions.append("done = 0")
    where = " AND ".join(conditions)
    rows = conn.execute(
        f"SELECT id, task, done, created_at, nudge_date FROM todos"
        f" WHERE {where} ORDER BY id",
        params,
    ).fetchall()
    conn.close()
    return [
        {"id": r["id"], "task": r["task"], "done": bool(r["done"]),
         "created_at": r["created_at"], "nudge_date": r["nudge_date"]}
        for r in rows
    ]


def normalize_todo_match(value: str) -> str:
    """Normalize only representation differences, not task meaning."""
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def find_todos_by_exact_match(
    user_id: int,
    match: str,
    *,
    done: bool | None = None,
) -> list[dict]:
    """Return owner-scoped todos whose normalized task exactly matches."""
    todos = get_todos(user_id, include_done=True)
    if done is not None:
        todos = [todo for todo in todos if todo["done"] is done]
    normalized_match = normalize_todo_match(match)
    return [
        todo
        for todo in todos
        if normalize_todo_match(todo["task"]) == normalized_match
    ]


def complete_todo(user_id: int, todo_id: int) -> bool:
    """Mark a todo as done. Returns True if updated."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    cursor = conn.execute(
        "UPDATE todos SET done = 1, completed_at = ? "
        "WHERE id = ? AND user_id = ? AND done = 0",
        (now, todo_id, user_id),
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def reopen_todo(user_id: int, todo_id: int) -> bool:
    """Mark a completed todo as active again."""
    conn = _connect()
    cursor = conn.execute(
        "UPDATE todos SET done = 0, completed_at = NULL "
        "WHERE id = ? AND user_id = ? AND done = 1",
        (todo_id, user_id),
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def delete_todo(user_id: int, todo_id: int) -> bool:
    """Delete a todo. Returns True if deleted."""
    conn = _connect()
    cursor = conn.execute(
        "DELETE FROM todos WHERE id = ? AND user_id = ?", (todo_id, user_id)
    )
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def update_todo(user_id: int, todo_id: int, **fields) -> str:
    """Update mutable fields and report updated, unchanged, or not_found.

    Supported fields: task, nudge_date.
    """
    allowed = {"task", "nudge_date"}
    to_set = {k: v for k, v in fields.items() if k in allowed}
    if not to_set:
        return "unchanged"
    conn = _connect()
    current = conn.execute(
        "SELECT task, nudge_date FROM todos WHERE id = ? AND user_id = ?",
        (todo_id, user_id),
    ).fetchone()
    if current is None:
        conn.close()
        return "not_found"
    if all(current[key] == value for key, value in to_set.items()):
        conn.close()
        return "unchanged"

    set_clause = ", ".join(f"{k} = ?" for k in to_set)
    params = list(to_set.values()) + [todo_id, user_id]
    conn.execute(
        f"UPDATE todos SET {set_clause} WHERE id = ? AND user_id = ?", params
    )
    conn.commit()
    conn.close()
    return "updated"


def get_visible_todos(user_id: int, today_str: str) -> list[dict]:
    """Return pending todos visible in diary: due today, overdue, or no date.

    Future todos (nudge_date > today) are excluded.
    """
    conn = _connect()
    rows = conn.execute(
        "SELECT id, user_id, task, nudge_date FROM todos "
        "WHERE user_id = ? AND done = 0 "
        "AND (nudge_date IS NULL OR nudge_date <= ?) "
        "ORDER BY nudge_date IS NULL, nudge_date, id",
        (user_id, today_str),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_active_todo_count(user_id: int) -> int:
    """Count active (not done) todos for a user."""
    conn = _connect()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM todos WHERE user_id = ? AND done = 0",
        (user_id,),
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0
