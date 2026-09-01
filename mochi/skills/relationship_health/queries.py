"""Relationship health skill — assessment snapshot storage.

Snapshots exist so momentum can be computed: a single RQI is a reading, and
only a series says whether things are getting better. One row per assessment,
never updated in place.
"""

import json
from datetime import datetime

from mochi.config import TZ
from mochi.db import _connect

DEFAULT_SUBJECT = "默认关系"
HISTORY_LIMIT = 20


def normalize_subject(raw: str | None) -> str:
    """Collapse a caller-supplied subject label to its stored form."""
    subject = " ".join((raw or "").split())
    return subject[:80] or DEFAULT_SUBJECT


def record_assessment(
    user_id: int,
    subject: str,
    *,
    rqi: float,
    tier: str,
    coverage: float,
    acs: float | None,
    llmi: float | None,
    dimensions: dict[str, float],
    note: str = "",
) -> int:
    """Store one assessment and return its row id."""
    conn = _connect()
    try:
        cursor = conn.execute(
            "INSERT INTO relationship_assessments "
            "(user_id, subject, rqi, tier, coverage, acs, llmi, "
            "dimensions_json, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                subject,
                float(rqi),
                tier,
                float(coverage),
                acs,
                llmi,
                json.dumps(dimensions, ensure_ascii=False, sort_keys=True),
                note[:500],
                datetime.now(TZ).isoformat(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def get_assessments(
    user_id: int,
    subject: str,
    limit: int = HISTORY_LIMIT,
) -> list[dict]:
    """Return assessments for one subject, oldest first.

    The newest ``limit`` rows are selected and then reversed, so a long history
    keeps its most recent window rather than its opening one.
    """
    limit = max(1, min(int(limit), 200))
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, subject, rqi, tier, coverage, acs, llmi, "
            "dimensions_json, note, created_at "
            "FROM relationship_assessments "
            "WHERE user_id = ? AND subject = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (user_id, subject, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in reversed(rows)]


def get_latest_assessment(user_id: int, subject: str) -> dict | None:
    """Return the newest assessment for one subject, or ``None``."""
    history = get_assessments(user_id, subject, limit=1)
    return history[-1] if history else None


def get_chat_transcript(
    user_id: int,
    *,
    since: str | None = None,
    limit: int = 40,
) -> list[dict]:
    """Return recent user/assistant turns, oldest first.

    ``since`` is an inclusive-exclusive ISO timestamp: only messages strictly
    after it are returned, so a morning run does not re-read the window that
    already produced the previous snapshot.
    """
    limit = max(1, min(int(limit), 80))
    conn = _connect()
    try:
        conditions = ["user_id = ?", "role IN ('user', 'assistant')"]
        params: list = [user_id]
        if since:
            conditions.append("created_at > ?")
            params.append(since)
        params.append(limit)
        rows = conn.execute(
            "SELECT role, content, created_at FROM messages WHERE "
            + " AND ".join(conditions)
            + " ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in reversed(rows)]


def list_subjects(user_id: int) -> list[dict]:
    """Return every subject with its assessment count and latest score."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT subject, COUNT(*) AS runs, MAX(created_at) AS latest_at "
            "FROM relationship_assessments WHERE user_id = ? "
            "GROUP BY subject ORDER BY latest_at DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]
