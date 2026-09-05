"""Validation and provenance helpers for durable Memory Items."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Sequence


MAX_MEMORY_CONTENT_CHARS = 160
MAX_EVIDENCE_MESSAGE_IDS = 20

# Canonical English keys stored in DB; Chinese labels for prompts / UI.
MEMORY_TAG_KEYS: tuple[str, ...] = (
    "fact",
    "emotion",
    "preference",
    "event",
    "habit",
    "relationship",
)
MEMORY_TAG_LABELS_ZH: dict[str, str] = {
    "fact": "事实",
    "emotion": "情感",
    "preference": "偏好",
    "event": "事件",
    "habit": "习惯",
    "relationship": "关系",
}
_MEMORY_TAG_ALIASES: dict[str, str] = {
    **{key: key for key in MEMORY_TAG_KEYS},
    **{label: key for key, label in MEMORY_TAG_LABELS_ZH.items()},
}
MEMORY_KINDS: tuple[str, ...] = ("core", "temp")


def normalize_memory_exact(content: str) -> str:
    """Normalize cosmetic representation while preserving meaningful symbols."""
    normalized = unicodedata.normalize("NFKC", content or "").casefold()
    return " ".join(normalized.split())


def validate_memory_content(content: object) -> str:
    if not isinstance(content, str):
        raise ValueError("memory content must be a string")
    cleaned = content.strip()
    if not 1 <= len(cleaned) <= MAX_MEMORY_CONTENT_CHARS:
        raise ValueError(
            f"memory content must contain 1-{MAX_MEMORY_CONTENT_CHARS} characters"
        )
    return cleaned


def validate_memory_importance(importance: object) -> int:
    if isinstance(importance, bool) or not isinstance(importance, int):
        raise ValueError("memory importance must be an integer")
    if importance not in (1, 2, 3):
        raise ValueError("memory importance must be 1, 2, or 3")
    return importance


def normalize_memory_tag(tag: object) -> str:
    if not isinstance(tag, str):
        raise ValueError("memory tag must be a string")
    key = _MEMORY_TAG_ALIASES.get(tag.strip())
    if key is None:
        raise ValueError(
            "memory tag must be one of: "
            + ", ".join(MEMORY_TAG_LABELS_ZH[k] for k in MEMORY_TAG_KEYS)
        )
    return key


def validate_memory_tags(tags: object) -> tuple[str, ...]:
    """Require at least one canonical tag; preserve first-seen order."""
    if isinstance(tags, (str, bytes)) or not isinstance(tags, Sequence):
        raise ValueError("memory tags must be an array")
    if not tags:
        raise ValueError("memory tags must contain at least one tag")
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        key = normalize_memory_tag(tag)
        if key not in seen:
            seen.add(key)
            normalized.append(key)
    return tuple(normalized)


def encode_memory_tags(tags: Sequence[str] | None) -> str:
    return json.dumps(
        list(validate_memory_tags(tags or ())),
        separators=(",", ":"),
        ensure_ascii=False,
    )


def decode_memory_tags(raw: str | None) -> tuple[str, ...]:
    if raw is None or raw == "":
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("stored memory tags contain invalid JSON") from exc
    if not isinstance(parsed, list):
        raise ValueError("stored memory tags must be a JSON array")
    if not parsed:
        return ()
    return validate_memory_tags(parsed)


def format_memory_tags_zh(tags: Sequence[str]) -> str:
    labels = [
        MEMORY_TAG_LABELS_ZH.get(tag, tag)
        for tag in tags
    ]
    return "、".join(labels)


def validate_memory_kind(kind: object) -> str:
    if not isinstance(kind, str) or kind.strip() not in MEMORY_KINDS:
        raise ValueError("memory kind must be 'core' or 'temp'")
    return kind.strip()


def infer_memory_tags(content: str) -> tuple[str, ...]:
    """Heuristic backfill when Lite tags are missing on legacy rows."""
    text = content or ""
    found: list[str] = []

    def add(key: str) -> None:
        if key not in found:
            found.append(key)

    preference_hits = (
        "喜欢", "爱吃", "不喜欢", "讨厌", "偏好", "习惯吃", "想要",
        "prefer", "hate", "love",
    )
    emotion_hits = (
        "开心", "难过", "焦虑", "害怕", "担心", "生气", "感动", "委屈",
        "低落", "情绪", "想哭",
    )
    habit_hits = (
        "每天", "总是", "经常", "习惯", "起床", "熬夜", "早睡",
    )
    relationship_hits = (
        "关系", "喜欢你", "爱你", "朋友", "家人", "恋爱", "心宿二",
    )
    event_hits = (
        "去了", "参加了", "发生了", "考过", "毕业", "搬家", "分手",
        "见面", "约会",
    )

    if any(token in text for token in preference_hits):
        add("preference")
    if any(token in text for token in emotion_hits):
        add("emotion")
    if any(token in text for token in habit_hits):
        add("habit")
    if any(token in text for token in relationship_hits):
        add("relationship")
    if any(token in text for token in event_hits):
        add("event")
    if not found:
        add("fact")
    return tuple(found)

def normalize_evidence_message_ids(
    message_ids: Sequence[int] | None,
) -> tuple[int, ...]:
    if message_ids is None:
        return ()
    if isinstance(message_ids, (str, bytes)) or not isinstance(
        message_ids, Sequence,
    ):
        raise ValueError("evidence message IDs must be an array")
    normalized: list[int] = []
    seen: set[int] = set()
    for message_id in message_ids:
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id <= 0
        ):
            raise ValueError("evidence message IDs must be positive integers")
        if message_id not in seen:
            seen.add(message_id)
            normalized.append(message_id)
    if len(normalized) > MAX_EVIDENCE_MESSAGE_IDS:
        raise ValueError(
            f"evidence message IDs exceed the {MAX_EVIDENCE_MESSAGE_IDS}-ID limit"
        )
    return tuple(normalized)


def encode_evidence_message_ids(
    message_ids: Sequence[int] | None,
) -> str:
    return json.dumps(
        normalize_evidence_message_ids(message_ids),
        separators=(",", ":"),
    )


def decode_evidence_message_ids(raw: str | None) -> tuple[int, ...]:
    if raw is None:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("stored evidence message IDs contain invalid JSON") from exc
    if not isinstance(parsed, list):
        raise ValueError("stored evidence message IDs must be a JSON array")
    return normalize_evidence_message_ids(parsed)


def merge_evidence_message_ids(
    *groups: Sequence[int],
) -> tuple[int, ...]:
    merged: list[int] = []
    seen: set[int] = set()
    for group in groups:
        for message_id in normalize_evidence_message_ids(group):
            if message_id not in seen:
                seen.add(message_id)
                merged.append(message_id)
    return tuple(merged[-MAX_EVIDENCE_MESSAGE_IDS:])
