"""Ephemeral Telegram visitor sessions.

Visitor turns read the owner's Core / diary / memories, but their own
messages never enter the messages table or memory extraction.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from mochi.config import TZ

_TTL_S = 45 * 60
_MAX_MESSAGES = 24

_sessions: dict[int, list[dict]] = {}
_touched: dict[int, float] = {}
_LOCK = threading.Lock()

VISITOR_MEDIA_TOOLS = ("send_sticker", "send_photo", "send_voice")


def visitor_history(user_id: int) -> list[dict]:
    """Return the in-memory visitor transcript, dropping expired sessions."""
    with _LOCK:
        _expire_locked()
        return list(_sessions.get(user_id, []))


def append_visitor_turn(
    user_id: int, user_content: str, assistant_content: str,
) -> None:
    """Record one delivered visitor exchange. Restarting the process forgets it."""
    now = datetime.now(TZ).isoformat()
    with _LOCK:
        _expire_locked()
        items = _sessions.setdefault(user_id, [])
        if user_content:
            items.append({
                "role": "user",
                "content": user_content,
                "created_at": now,
                "processed": False,
            })
        if assistant_content:
            items.append({
                "role": "assistant",
                "content": assistant_content,
                "created_at": now,
                "processed": False,
            })
        if len(items) > _MAX_MESSAGES:
            _sessions[user_id] = items[-_MAX_MESSAGES:]
        _touched[user_id] = time.monotonic()


def clear_visitor_sessions() -> None:
    """Test helper."""
    with _LOCK:
        _sessions.clear()
        _touched.clear()


def _expire_locked() -> None:
    deadline = time.monotonic() - _TTL_S
    expired = [uid for uid, seen in _touched.items() if seen < deadline]
    for uid in expired:
        _sessions.pop(uid, None)
        _touched.pop(uid, None)
