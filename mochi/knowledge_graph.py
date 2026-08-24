"""Small user-life relationship graph maintained by Main each week."""

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from mochi.db import _connect
from mochi.config import TZ
from mochi.memory_contract import decode_evidence_message_ids

ALLOWED_ENTITY_TYPES = frozenset({"person", "pet", "place"})
ALLOWED_PREDICATES = frozenset({
    "is_family_of",
    "is_partner_of",
    "is_parent_of",
    "is_child_of",
    "is_sibling_of",
    "is_friend_of",
    "lives_with",
    "cares_for",
    "lives_in",
    "grew_up_in",
    "works_at",
})
MAX_RELATIONSHIP_OPERATIONS = 20

_RELATIONSHIP_TEMPLATES = {
    "is_family_of": "{subject} 和 {object} 是家人",
    "is_partner_of": "{subject} 的伴侣是 {object}",
    "is_parent_of": "{subject} 是 {object} 的家长",
    "is_child_of": "{subject} 是 {object} 的孩子",
    "is_sibling_of": "{subject} 和 {object} 是兄弟姐妹",
    "is_friend_of": "{subject} 和 {object} 是朋友",
    "lives_with": "{subject} 和 {object} 住在一起",
    "cares_for": "{subject} 在照顾 {object}",
    "lives_in": "{subject} 住在 {object}",
    "grew_up_in": "{subject} 在 {object} 长大",
    "works_at": "{subject} 在 {object} 工作",
}


class RelationshipCurationError(ValueError):
    """The requested relationship curation is outside the Weekly scope."""


class RelationshipCurationConflict(RelationshipCurationError):
    """A Memory Item or relationship changed after Main saw its snapshot."""


@dataclass(frozen=True)
class RelationshipCurationResult:
    upserted_ids: tuple[int, ...]
    archived_ids: tuple[int, ...]


def format_relationship(subject: str, predicate: str, object_: str) -> str:
    template = _RELATIONSHIP_TEMPLATES.get(predicate)
    if template:
        return template.format(subject=subject, object=object_)
    return f"{subject} 与 {object_} 的关系是 {predicate.replace('_', ' ')}"


def format_relationship_snapshot(snapshot: dict) -> str:
    return format_relationship(
        str(snapshot.get("subject") or "?"),
        str(snapshot.get("predicate") or ""),
        str(snapshot.get("object") or "?"),
    )

# Emoji pattern: common animal/object emoji + supplementary plane symbols
_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001F9FF\U00002600-\U000027BF\U0000FE00-\U0000FE0F"
    r"\U0000200D\U00002702-\U000027B0\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF]+",
)


def _normalize_name(name: str) -> str:
    """Normalize entity name for canonical storage and matching.

    Strips emoji, normalizes unicode, lowercases, collapses whitespace.
    """
    name = unicodedata.normalize("NFKC", name)
    name = _EMOJI_RE.sub("", name)
    name = re.sub(r"[()（）]", "", name)
    name = re.sub(r"\s+", " ", name).strip().lower()
    return name


def get_entity_by_name(user_id: int, name: str) -> dict | None:
    """Lookup entity by normalized name. Returns dict or None."""
    canonical = _normalize_name(name)
    if not canonical:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, user_id, name, display_name, entity_type, created_at "
            "FROM kg_entities WHERE user_id = ? AND name = ?",
            (user_id, canonical),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_active_relationships(user_id: int) -> list[dict]:
    """Return exact snapshots that Weekly Main may archive."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT t.id AS triple_id, s.display_name AS subject, "
            "s.entity_type AS subject_type, t.predicate, "
            "o.display_name AS object, o.entity_type AS object_type, "
            "t.source_memory_id, t.created_at "
            "FROM kg_triples t "
            "JOIN kg_entities s ON s.id = t.subject_id "
            "JOIN kg_entities o ON o.id = t.object_id "
            "WHERE t.user_id = ? AND t.valid_to IS NULL "
            "ORDER BY t.id",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _parse_entity(raw: object, field: str) -> dict:
    if not isinstance(raw, dict) or set(raw) != {"name", "type"}:
        raise RelationshipCurationError(
            f"{field} must contain exactly name and type"
        )
    name = raw["name"]
    entity_type = raw["type"]
    if not isinstance(name, str) or not _normalize_name(name):
        raise RelationshipCurationError(f"{field} name is invalid")
    if entity_type not in ALLOWED_ENTITY_TYPES:
        raise RelationshipCurationError(
            f"{field} type must be person, pet, or place"
        )
    return {"name": name.strip(), "type": entity_type}


def _entity_id(conn, user_id: int, entity: dict, now: str) -> int:
    canonical = _normalize_name(entity["name"])
    row = conn.execute(
        "SELECT id, entity_type FROM kg_entities "
        "WHERE user_id = ? AND name = ?",
        (user_id, canonical),
    ).fetchone()
    if row:
        if row["entity_type"] != entity["type"]:
            raise RelationshipCurationConflict(
                f"{entity['name']} already exists with a different type"
            )
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO kg_entities "
        "(user_id, name, display_name, entity_type, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, canonical, entity["name"], entity["type"], now),
    )
    return int(cursor.lastrowid)


def _validate_memory_snapshot(
    conn,
    user_id: int,
    visible_memory_snapshots: Mapping[int, Mapping[str, object]],
    raw: object,
) -> int:
    if not isinstance(raw, dict) or set(raw) != {"item_id"}:
        raise RelationshipCurationError(
            "source_memory must contain exactly item_id"
        )
    item_id = raw["item_id"]
    if (
        not isinstance(item_id, int)
        or isinstance(item_id, bool)
        or item_id not in visible_memory_snapshots
    ):
        raise RelationshipCurationError(
            "source_memory must be a Memory Item visible this week"
        )
    snapshot = visible_memory_snapshots[item_id]
    row = conn.execute(
        "SELECT content, updated_at, evidence_message_ids "
        "FROM memory_items WHERE id = ? AND user_id = ?",
        (item_id, user_id),
    ).fetchone()
    if (
        row is None
        or snapshot.get("content") != row["content"]
        or snapshot.get("updated_at") != row["updated_at"]
    ):
        raise RelationshipCurationConflict(
            "source Memory Item changed after Weekly context was built"
        )
    evidence_ids = decode_evidence_message_ids(row["evidence_message_ids"])
    if not evidence_ids:
        raise RelationshipCurationError(
            "source Memory Item must have user-message evidence"
        )
    placeholders = ",".join("?" * len(evidence_ids))
    count = conn.execute(
        f"SELECT COUNT(*) FROM messages WHERE user_id = ? AND role = 'user' "
        f"AND id IN ({placeholders})",
        (user_id, *evidence_ids),
    ).fetchone()[0]
    if count != len(evidence_ids):
        raise RelationshipCurationError(
            "source Memory Item evidence is no longer valid"
        )
    return item_id


def _load_relationship_snapshot(conn, user_id: int, triple_id: int) -> dict | None:
    row = conn.execute(
        "SELECT t.id AS triple_id, s.display_name AS subject, "
        "s.entity_type AS subject_type, t.predicate, "
        "o.display_name AS object, o.entity_type AS object_type, "
        "t.source_memory_id, t.created_at "
        "FROM kg_triples t "
        "JOIN kg_entities s ON s.id = t.subject_id "
        "JOIN kg_entities o ON o.id = t.object_id "
        "WHERE t.id = ? AND t.user_id = ? AND t.valid_to IS NULL",
        (triple_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def _find_visible_relationship(
    snapshots: Mapping[int, Mapping[str, object]],
    subject: dict,
    predicate: str,
    object_: dict,
) -> Mapping[str, object] | None:
    subject_name = _normalize_name(subject["name"])
    object_name = _normalize_name(object_["name"])
    for snapshot in snapshots.values():
        if (
            _normalize_name(str(snapshot.get("subject", ""))) == subject_name
            and snapshot.get("subject_type") == subject["type"]
            and snapshot.get("predicate") == predicate
            and _normalize_name(str(snapshot.get("object", ""))) == object_name
            and snapshot.get("object_type") == object_["type"]
        ):
            return snapshot
    return None


def _cleanup_orphan_entities(conn, user_id: int) -> None:
    conn.execute(
        "DELETE FROM kg_entities WHERE user_id = ? "
        "AND NOT EXISTS ("
        "SELECT 1 FROM kg_triples t "
        "WHERE t.subject_id = kg_entities.id OR t.object_id = kg_entities.id"
        ")",
        (user_id,),
    )


def curate_relationships(
    user_id: int,
    visible_memory_snapshots: Mapping[int, Mapping[str, object]],
    visible_relationship_snapshots: Mapping[int, Mapping[str, object]],
    operations: object,
) -> RelationshipCurationResult:
    """Apply one exact, evidence-backed Weekly relationship batch atomically."""
    if not isinstance(operations, list):
        raise RelationshipCurationError("operations must be an array")
    if len(operations) > MAX_RELATIONSHIP_OPERATIONS:
        raise RelationshipCurationError("too many relationship operations")

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = datetime.now(TZ).isoformat()
        upserted: list[int] = []
        archived: list[int] = []
        for operation in operations:
            if not isinstance(operation, dict):
                raise RelationshipCurationError("each operation must be an object")
            op = operation.get("op")
            if op == "upsert":
                if set(operation) != {
                    "op", "subject", "predicate", "object", "source_memory",
                }:
                    raise RelationshipCurationError(
                        "upsert fields do not match the tool contract"
                    )
                subject = _parse_entity(operation["subject"], "subject")
                object_ = _parse_entity(operation["object"], "object")
                predicate = operation["predicate"]
                if predicate not in ALLOWED_PREDICATES:
                    raise RelationshipCurationError(
                        "unsupported relationship predicate"
                    )
                expected_relationship = _find_visible_relationship(
                    visible_relationship_snapshots,
                    subject,
                    predicate,
                    object_,
                )
                if expected_relationship is not None:
                    current_relationship = _load_relationship_snapshot(
                        conn,
                        user_id,
                        int(expected_relationship["triple_id"]),
                    )
                    if current_relationship != expected_relationship:
                        raise RelationshipCurationConflict(
                            "relationship changed after Weekly context was built"
                        )
                memory_id = _validate_memory_snapshot(
                    conn, user_id, visible_memory_snapshots,
                    operation["source_memory"],
                )
                subject_id = _entity_id(conn, user_id, subject, now)
                object_id = _entity_id(conn, user_id, object_, now)
                existing = conn.execute(
                    "SELECT id FROM kg_triples WHERE user_id = ? "
                    "AND subject_id = ? AND predicate = ? AND object_id = ? "
                    "AND valid_to IS NULL",
                    (user_id, subject_id, predicate, object_id),
                ).fetchone()
                if existing:
                    if expected_relationship is None:
                        raise RelationshipCurationConflict(
                            "relationship appeared after Weekly context was built"
                        )
                    cursor = conn.execute(
                        "UPDATE kg_triples SET source_memory_id = ?, "
                        "source = 'weekly_main', confidence = 1.0 "
                        "WHERE id = ? AND source_memory_id IS NOT ?",
                        (memory_id, existing["id"], memory_id),
                    )
                    if cursor.rowcount:
                        upserted.append(int(existing["id"]))
                    continue
                cursor = conn.execute(
                    "INSERT INTO kg_triples "
                    "(user_id, subject_id, predicate, object_id, "
                    "source_memory_id, source, confidence, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'weekly_main', 1.0, ?)",
                    (
                        user_id, subject_id, predicate, object_id,
                        memory_id, now,
                    ),
                )
                upserted.append(int(cursor.lastrowid))
                continue

            if op == "archive":
                if set(operation) != {"op", "triple_id"}:
                    raise RelationshipCurationError(
                        "archive fields do not match the tool contract"
                    )
                triple_id = operation["triple_id"]
                if (
                    isinstance(triple_id, bool)
                    or not isinstance(triple_id, int)
                    or triple_id not in visible_relationship_snapshots
                ):
                    raise RelationshipCurationError(
                        "triple_id must reference a visible active relationship"
                    )
                expected = visible_relationship_snapshots[triple_id]
                current_relationship = _load_relationship_snapshot(
                    conn, user_id, triple_id,
                )
                if current_relationship != expected:
                    raise RelationshipCurationConflict(
                        "relationship changed after Weekly context was built"
                    )
                cursor = conn.execute(
                    "UPDATE kg_triples SET valid_to = ? "
                    "WHERE id = ? AND valid_to IS NULL",
                    (now, triple_id),
                )
                if cursor.rowcount != 1:
                    raise RelationshipCurationConflict(
                        "relationship changed before archive committed"
                    )
                archived.append(triple_id)
                continue

            raise RelationshipCurationError("op must be upsert or archive")

        _cleanup_orphan_entities(conn, user_id)
        conn.commit()
        return RelationshipCurationResult(
            upserted_ids=tuple(upserted),
            archived_ids=tuple(archived),
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Query ─────────────────────────────────────────────────────────────


def query_entity(
    user_id: int, name: str, as_of: str | None = None,
    limit: int | None = None,
) -> dict | None:
    """Get all relationships for an entity.

    Returns:
        {"entity": {...}, "as_subject": [...], "as_object": [...]}
        or None if entity not found.
    """
    from mochi.config import KG_MAX_TRIPLES_PER_ENTITY
    limit = limit or KG_MAX_TRIPLES_PER_ENTITY

    entity = get_entity_by_name(user_id, name)
    if not entity:
        return None

    eid = entity["id"]
    conn = _connect()
    try:
        if as_of:
            time_filter = (
                "AND (t.valid_from IS NULL OR t.valid_from <= ?) "
                "AND (t.valid_to IS NULL OR t.valid_to >= ?)"
            )
            time_params = (as_of, as_of)
        else:
            time_filter = "AND t.valid_to IS NULL"
            time_params = ()

        as_subject = conn.execute(
            f"SELECT t.id, t.predicate, t.valid_from, t.valid_to, t.confidence, "
            f"  e.name AS object_name, e.display_name AS object_display, "
            f"  e.entity_type AS object_type "
            f"FROM kg_triples t "
            f"JOIN kg_entities e ON e.id = t.object_id "
            f"WHERE t.subject_id = ? {time_filter} "
            f"ORDER BY t.created_at DESC LIMIT ?",
            (eid, *time_params, limit),
        ).fetchall()

        as_object = conn.execute(
            f"SELECT t.id, t.predicate, t.valid_from, t.valid_to, t.confidence, "
            f"  e.name AS subject_name, e.display_name AS subject_display, "
            f"  e.entity_type AS subject_type "
            f"FROM kg_triples t "
            f"JOIN kg_entities e ON e.id = t.subject_id "
            f"WHERE t.object_id = ? {time_filter} "
            f"ORDER BY t.created_at DESC LIMIT ?",
            (eid, *time_params, limit),
        ).fetchall()

        return {
            "entity": entity,
            "as_subject": [dict(r) for r in as_subject],
            "as_object": [dict(r) for r in as_object],
        }
    finally:
        conn.close()


def entity_context_for_prompt(user_id: int, entity_name: str) -> str:
    """Format entity relationships as compact text for prompt injection.

    Returns empty string if no data found or entity unknown.
    Token-limited by KG_MAX_ENTITY_CONTEXT_TOKENS config.
    """
    from mochi.config import KG_MAX_ENTITY_CONTEXT_TOKENS

    result = query_entity(user_id, entity_name)
    if not result:
        return ""

    entity = result["entity"]
    display = entity.get("display_name", entity.get("name", "?"))
    relationships: list[str] = []

    for tri in result["as_subject"]:
        other = tri.get("object_display") or tri.get("object_name", "?")
        relationships.append(
            format_relationship(display, tri["predicate"], other)
        )

    for tri in result["as_object"]:
        other = tri.get("subject_display") or tri.get("subject_name", "?")
        relationships.append(
            format_relationship(other, tri["predicate"], display)
        )

    relationships = list(dict.fromkeys(relationships))
    if not relationships:
        return ""

    from mochi.token_estimator import estimate_tokens

    header = "已知关系："
    max_tokens = max(1, KG_MAX_ENTITY_CONTEXT_TOKENS)
    selected: list[str] = []
    for relationship in relationships:
        proposed = "\n".join([
            header,
            *(f"- {item}" for item in [*selected, relationship]),
        ])
        if estimate_tokens(proposed) > max_tokens:
            continue
        selected.append(relationship)
    if not selected:
        return ""

    omitted = len(relationships) - len(selected)
    lines = [header, *(f"- {item}" for item in selected)]
    if omitted:
        marker = f"- [另有 {omitted} 条关系因上下文预算未显示]"
        if estimate_tokens("\n".join([*lines, marker])) <= max_tokens:
            lines.append(marker)
    return "\n".join(lines)


def find_matching_entities(
    user_id: int, text: str,
    matchable_types: tuple[str, ...] = ("person", "pet", "place"),
) -> list[str]:
    """Find known entity names that appear in text.

    Only matches entities of matchable_types to reduce false positives
    (e.g., common words that happen to be entity names).
    """
    from mochi.config import KG_ENTITY_MATCH_MIN_LENGTH
    min_len = max(1, KG_ENTITY_MATCH_MIN_LENGTH)

    conn = _connect()
    try:
        placeholders = ",".join("?" for _ in matchable_types)
        rows = conn.execute(
            f"SELECT name, display_name FROM kg_entities "
            f"WHERE user_id = ? AND entity_type IN ({placeholders}) "
            f"AND LENGTH(name) >= ? "
            f"AND EXISTS ("
            f"SELECT 1 FROM kg_triples t "
            f"WHERE t.user_id = kg_entities.user_id "
            f"AND t.valid_to IS NULL "
            f"AND (t.subject_id = kg_entities.id OR t.object_id = kg_entities.id)"
            f")",
            (user_id, *matchable_types, min_len),
        ).fetchall()
    finally:
        conn.close()

    text_lower = text.lower()
    matched = []
    for row in rows:
        if row["name"] in text_lower:
            matched.append(row["name"])
    return matched


# ── Stats & Cleanup ──────────────────────────────────────────────────


def get_kg_stats(user_id: int) -> dict:
    """Return KG statistics for diagnostics."""
    conn = _connect()
    try:
        entities = conn.execute(
            "SELECT COUNT(DISTINCT entity_id) FROM ("
            "SELECT subject_id AS entity_id FROM kg_triples "
            "WHERE user_id = ? AND valid_to IS NULL "
            "UNION ALL "
            "SELECT object_id AS entity_id FROM kg_triples "
            "WHERE user_id = ? AND valid_to IS NULL"
            ")",
            (user_id, user_id),
        ).fetchone()[0]
        active_triples = conn.execute(
            "SELECT COUNT(*) FROM kg_triples "
            "WHERE user_id = ? AND valid_to IS NULL",
            (user_id,),
        ).fetchone()[0]
        total_triples = conn.execute(
            "SELECT COUNT(*) FROM kg_triples WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
        return {
            "entities": entities,
            "active_triples": active_triples,
            "total_triples": total_triples,
        }
    finally:
        conn.close()


def cleanup_expired_triples(days: int = 90) -> int:
    """Hard-delete triples whose valid_to is older than N days. Returns count purged."""
    cutoff = (datetime.now(TZ) - timedelta(days=days)).isoformat()
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM kg_triples WHERE valid_to IS NOT NULL AND valid_to < ?",
            (cutoff,),
        )
        count = cur.rowcount
        conn.execute(
            "DELETE FROM kg_entities WHERE NOT EXISTS ("
            "SELECT 1 FROM kg_triples t "
            "WHERE t.subject_id = kg_entities.id OR t.object_id = kg_entities.id"
            ")"
        )
        conn.commit()
        return count
    finally:
        conn.close()
