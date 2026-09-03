"""photo_refs table helpers."""

from __future__ import annotations

from datetime import datetime

from mochi.config import TZ
from mochi.db import _connect

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS photo_refs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    region      TEXT    DEFAULT '',
    tags        TEXT    DEFAULT '',
    caption     TEXT    DEFAULT '',
    source_url  TEXT    DEFAULT '',
    created_at  TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_photo_refs_source
    ON photo_refs(source_url) WHERE source_url != '';
CREATE INDEX IF NOT EXISTS idx_photo_refs_kind_region
    ON photo_refs(kind, region);
CREATE TABLE IF NOT EXISTS photo_sends (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    logical_date  TEXT    NOT NULL,
    bucket        TEXT    NOT NULL,
    turn_id       TEXT    DEFAULT '',
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photo_sends_day
    ON photo_sends(user_id, logical_date, bucket);
"""


def init_photo_refs_schema(conn) -> None:
    conn.executescript(SCHEMA_SQL)


def source_url_exists(source_url: str) -> bool:
    if not source_url:
        return False
    conn = _connect()
    row = conn.execute(
        "SELECT 1 FROM photo_refs WHERE source_url = ? LIMIT 1",
        (source_url,),
    ).fetchone()
    conn.close()
    return row is not None


def count_scene_refs(region: str = "") -> int:
    conn = _connect()
    if region:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM photo_refs WHERE kind = 'scene' AND region = ?",
            (region,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM photo_refs WHERE kind = 'scene'",
        ).fetchone()
    conn.close()
    return int(row["n"] if row else 0)


def insert_photo_ref(
    *,
    filename: str,
    kind: str,
    region: str = "",
    tags: str = "",
    caption: str = "",
    source_url: str = "",
) -> int | None:
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    try:
        cursor = conn.execute(
            "INSERT INTO photo_refs "
            "(filename, kind, region, tags, caption, source_url, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (filename, kind, region, tags, caption, source_url, now),
        )
        conn.commit()
        return int(cursor.lastrowid)
    except Exception:
        conn.rollback()
        return None
    finally:
        conn.close()


def list_photo_refs(
    *,
    kind: str = "",
    region: str = "",
    limit: int = 40,
) -> list[dict]:
    clauses = []
    args: list[object] = []
    if kind:
        clauses.append("kind = ?")
        args.append(kind)
    if region:
        clauses.append("region = ?")
        args.append(region)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = _connect()
    rows = conn.execute(
        f"SELECT id, filename, kind, region, tags, caption, source_url, created_at "
        f"FROM photo_refs {where} ORDER BY id DESC LIMIT ?",
        (*args, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_photo_sends(user_id: int, logical_date: str, bucket: str) -> int:
    conn = _connect()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM photo_sends "
        "WHERE user_id = ? AND logical_date = ? AND bucket = ?",
        (user_id, logical_date, bucket),
    ).fetchone()
    conn.close()
    return int(row["n"] if row else 0)


def record_photo_send(
    user_id: int,
    logical_date: str,
    bucket: str,
    turn_id: str = "",
) -> None:
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    conn.execute(
        "INSERT INTO photo_sends "
        "(user_id, logical_date, bucket, turn_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, logical_date, bucket, turn_id or "", now),
    )
    conn.commit()
    conn.close()
