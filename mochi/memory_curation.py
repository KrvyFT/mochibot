"""Bounded Weekly Memory candidates and guarded atomic curation."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime

from mochi import config
from mochi.db import (
    _connect,
    _delete_memory_item_indexes,
    _insert_memory_trash_snapshot,
    _invalidate_memory_kg_indexes,
    _normalize_text,
    _sync_memory_item_indexes,
    insert_memory_item,
    text_similarity,
)
from mochi.memory_contract import (
    decode_evidence_message_ids,
    encode_evidence_message_ids,
    merge_evidence_message_ids,
    normalize_evidence_message_ids,
    validate_memory_content,
    validate_memory_importance,
)


WINDOW_ITEM_LIMIT = 40
RELATED_ITEM_LIMIT = 40
RELATED_SIMILARITY_THRESHOLD = 0.72
MIN_CONTAINMENT_CHARS = 4
EXCERPTS_PER_ITEM = 2
PACKAGE_EXCERPT_LIMIT = 80
EXCERPT_CHAR_LIMIT = 200
MAX_CURATION_OPERATIONS = 20


class MemoryCurationError(ValueError):
    """The candidate scope or requested curation is invalid."""


class MemoryCurationConflict(MemoryCurationError):
    """A Memory Item changed after its candidate snapshot."""


@dataclass(frozen=True)
class MemoryEvidenceExcerpt:
    message_id: int
    content: str
    created_at: str


@dataclass(frozen=True)
class WeeklyMemoryCandidate:
    id: int
    content: str
    importance: int
    source: str
    created_at: str
    updated_at: str
    evidence_message_ids: tuple[int, ...]
    evidence_excerpts: tuple[MemoryEvidenceExcerpt, ...]


@dataclass(frozen=True)
class WeeklyMemoryCandidatePackage:
    user_id: int
    window_start: str
    window_end: str
    window_items: tuple[WeeklyMemoryCandidate, ...]
    related_items: tuple[WeeklyMemoryCandidate, ...]
    window_total: int
    related_eligible_total: int
    window_truncated: bool
    related_truncated: bool
    allowed_item_ids: frozenset[int]
    allowed_evidence_message_ids: frozenset[int]


@dataclass(frozen=True)
class MemoryCurationResult:
    created_ids: tuple[int, ...]
    changed_ids: tuple[int, ...]
    archived_ids: tuple[int, ...]
    replayed: bool = False


def _relation_score(older: dict, window_rows: list[dict]) -> float | None:
    older_norm = _normalize_text(older["content"])
    best = 0.0
    eligible = False
    for current in window_rows:
        current_norm = _normalize_text(current["content"])
        shorter = min(len(older_norm), len(current_norm))
        containment = bool(
            shorter >= MIN_CONTAINMENT_CHARS
            and (older_norm in current_norm or current_norm in older_norm)
        )
        similarity = text_similarity(older["content"], current["content"])
        if containment or similarity >= RELATED_SIMILARITY_THRESHOLD:
            eligible = True
        best = max(best, similarity + (0.2 if containment else 0.0))
    return best if eligible else None


def _load_evidence(conn, user_id: int, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, content, created_at FROM messages "
        f"WHERE user_id = ? AND role = 'user' AND id IN ({placeholders})",
        [user_id, *ids],
    ).fetchall()
    return {row["id"]: dict(row) for row in rows}


def build_weekly_memory_candidate_package(
    user_id: int,
    start: datetime,
    end: datetime,
) -> WeeklyMemoryCandidatePackage:
    """Return a read-only, immutable, bounded Weekly candidate snapshot."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 0:
        raise ValueError("user_id must be a non-negative integer")
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("weekly window must be timezone-aware and increasing")
    start_iso = start.astimezone(config.TZ).isoformat()
    end_iso = end.astimezone(config.TZ).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN")
        window_total = conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE user_id = ? "
            "AND julianday(created_at) >= julianday(?) "
            "AND julianday(created_at) < julianday(?)",
            (user_id, start_iso, end_iso),
        ).fetchone()[0]
        window_rows = [
            dict(row) for row in conn.execute(
                "SELECT id, content, importance, source, "
                "evidence_message_ids, created_at, updated_at "
                "FROM memory_items WHERE user_id = ? "
                "AND julianday(created_at) >= julianday(?) "
                "AND julianday(created_at) < julianday(?) "
                "ORDER BY julianday(created_at) DESC, id DESC LIMIT ?",
                (user_id, start_iso, end_iso, WINDOW_ITEM_LIMIT),
            ).fetchall()
        ]
        related_ranked: list[tuple[float, dict]] = []
        if window_rows:
            cursor = conn.execute(
                "SELECT id, content, importance, source, "
                "evidence_message_ids, created_at, updated_at "
                "FROM memory_items WHERE user_id = ? "
                "AND julianday(created_at) < julianday(?) "
                "ORDER BY importance DESC, julianday(updated_at) DESC, id DESC",
                (user_id, start_iso),
            )
            while True:
                chunk = cursor.fetchmany(400)
                if not chunk:
                    break
                for row in chunk:
                    older = dict(row)
                    score = _relation_score(older, window_rows)
                    if score is not None:
                        related_ranked.append((score, older))
        related_eligible_total = len(related_ranked)
        related_rows = [
            row for _, row in sorted(
                related_ranked,
                key=lambda item: (
                    item[0],
                    item[1]["importance"],
                    item[1]["updated_at"],
                    item[1]["id"],
                ),
                reverse=True,
            )[:RELATED_ITEM_LIMIT]
        ]

        ordered_rows = window_rows + related_rows
        decoded: list[tuple[int, ...]] = []
        evidence_ids: list[int] = []
        for row in ordered_rows:
            ids = decode_evidence_message_ids(row["evidence_message_ids"])
            decoded.append(ids)
            for message_id in ids:
                if message_id not in evidence_ids:
                    evidence_ids.append(message_id)
        evidence = _load_evidence(conn, user_id, evidence_ids)
        allowed_evidence: set[int] = set()
        candidates: list[WeeklyMemoryCandidate] = []
        for row, stored_ids in zip(ordered_rows, decoded):
            valid_ids = tuple(item for item in stored_ids if item in evidence)
            excerpts = []
            for message_id in valid_ids:
                if len(excerpts) >= EXCERPTS_PER_ITEM:
                    break
                if (
                    message_id not in allowed_evidence
                    and len(allowed_evidence) >= PACKAGE_EXCERPT_LIMIT
                ):
                    continue
                allowed_evidence.add(message_id)
                message = evidence[message_id]
                excerpts.append(MemoryEvidenceExcerpt(
                    message_id=message_id,
                    content=message["content"][:EXCERPT_CHAR_LIMIT],
                    created_at=message["created_at"],
                ))
            candidates.append(WeeklyMemoryCandidate(
                id=row["id"],
                content=row["content"],
                importance=row["importance"],
                source=row["source"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                evidence_message_ids=valid_ids,
                evidence_excerpts=tuple(excerpts),
            ))
        window_items = tuple(candidates[:len(window_rows)])
        related_items = tuple(candidates[len(window_rows):])
        conn.rollback()
        return WeeklyMemoryCandidatePackage(
            user_id=user_id,
            window_start=start_iso,
            window_end=end_iso,
            window_items=window_items,
            related_items=related_items,
            window_total=window_total,
            related_eligible_total=related_eligible_total,
            window_truncated=window_total > len(window_items),
            related_truncated=related_eligible_total > len(related_items),
            allowed_item_ids=frozenset(
                item.id for item in (*window_items, *related_items)
            ),
            allowed_evidence_message_ids=frozenset(allowed_evidence),
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _exact(raw: object, keys: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping) or set(raw) != keys:
        raise MemoryCurationError(f"{label} has invalid fields")
    return raw


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MemoryCurationError(f"{label} must be a positive integer")
    return value


def _evidence(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise MemoryCurationError("evidence_message_ids must be an array")
    normalized = normalize_evidence_message_ids(value)
    if len(normalized) != len(value):
        raise MemoryCurationError("evidence_message_ids must not contain duplicates")
    return normalized


def _output(operation: Mapping[str, object]) -> dict:
    try:
        return {
            "content": validate_memory_content(operation["content"]),
            "importance": validate_memory_importance(operation["importance"]),
        }
    except ValueError as exc:
        raise MemoryCurationError(str(exc)) from exc


def _parse_operations(operations: object) -> tuple[list[dict], set[int]]:
    if not isinstance(operations, list):
        raise MemoryCurationError("operations must be an array")
    if len(operations) > MAX_CURATION_OPERATIONS:
        raise MemoryCurationError("too many curation operations")
    parsed: list[dict] = []
    touched: set[int] = set()
    for index, raw in enumerate(operations):
        if not isinstance(raw, Mapping):
            raise MemoryCurationError(f"operation {index} must be an object")
        op = raw.get("op")
        if op == "create":
            operation = _exact(
                raw,
                {"op", "content", "importance", "evidence_message_ids"},
                f"operation {index}",
            )
            parsed.append({
                "op": op,
                **_output(operation),
                "evidence": _evidence(operation["evidence_message_ids"]),
            })
            continue
        if op in {"edit", "archive"}:
            keys = {"op", "item_id", "evidence_message_ids"}
            if op == "edit":
                keys |= {"content", "importance"}
            operation = _exact(raw, keys, f"operation {index}")
            item_id = _positive_int(operation["item_id"], "item_id")
            parsed_operation = {
                "op": op,
                "item_id": item_id,
                "evidence": _evidence(operation["evidence_message_ids"]),
            }
            if op == "edit":
                parsed_operation.update(_output(operation))
            item_ids = [item_id]
        elif op == "merge":
            operation = _exact(
                raw,
                {
                    "op", "keep_item_id", "remove_item_ids", "content",
                    "importance", "evidence_message_ids",
                },
                f"operation {index}",
            )
            keep_id = _positive_int(operation["keep_item_id"], "keep_item_id")
            remove_raw = operation["remove_item_ids"]
            if not isinstance(remove_raw, list) or not remove_raw:
                raise MemoryCurationError(
                    "remove_item_ids must be a non-empty array"
                )
            remove_ids = tuple(
                _positive_int(item_id, "remove_item_ids item")
                for item_id in remove_raw
            )
            item_ids = [keep_id, *remove_ids]
            if len(item_ids) != len(set(item_ids)):
                raise MemoryCurationError("merge contains duplicate item IDs")
            parsed_operation = {
                "op": op,
                "keep_item_id": keep_id,
                "remove_item_ids": remove_ids,
                **_output(operation),
                "evidence": _evidence(operation["evidence_message_ids"]),
            }
        else:
            raise MemoryCurationError(f"unsupported operation {op!r}")
        for item_id in item_ids:
            if item_id in touched:
                raise MemoryCurationError(
                    f"Memory Item {item_id} is touched more than once"
                )
            touched.add(item_id)
        parsed.append(parsed_operation)
    return parsed, touched


def _result_payload(result: MemoryCurationResult) -> dict:
    return {
        "created_ids": list(result.created_ids),
        "changed_ids": list(result.changed_ids),
        "archived_ids": list(result.archived_ids),
    }


def _result_from_payload(payload: dict) -> MemoryCurationResult:
    return MemoryCurationResult(
        created_ids=tuple(payload["created_ids"]),
        changed_ids=tuple(payload["changed_ids"]),
        archived_ids=tuple(payload["archived_ids"]),
        replayed=True,
    )


def curate_memory_items(
    user_id: int,
    candidate_package: WeeklyMemoryCandidatePackage,
    allowed_evidence_message_ids: Collection[int],
    operations: object,
    *,
    period_key: str,
) -> MemoryCurationResult:
    """Apply one candidate-scoped curation batch in a single transaction."""
    if candidate_package.user_id != user_id:
        raise MemoryCurationError("candidate package belongs to another user")
    candidates = (
        *candidate_package.window_items,
        *candidate_package.related_items,
    )
    snapshots_by_id = {candidate.id: candidate for candidate in candidates}
    allowed_items = frozenset(snapshots_by_id)
    allowed_evidence = frozenset(allowed_evidence_message_ids)
    parsed, touched = _parse_operations(operations)
    if not touched <= allowed_items:
        raise MemoryCurationError("referenced Memory Items are outside Weekly scope")
    requested_evidence = {
        message_id
        for operation in parsed
        for message_id in operation["evidence"]
    }
    if not requested_evidence <= allowed_evidence:
        raise MemoryCurationError("cited evidence is outside Weekly scope")

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing_receipt = conn.execute(
            "SELECT result_json FROM weekly_curation_batches "
            "WHERE user_id = ? AND period_key = ?",
            (user_id, period_key),
        ).fetchone()
        if existing_receipt:
            conn.rollback()
            return _result_from_payload(json.loads(existing_receipt["result_json"]))

        rows_by_id = {}
        if touched:
            placeholders = ",".join("?" * len(touched))
            rows = conn.execute(
                f"SELECT id, user_id, category, content, importance, source, "
                f"evidence_message_ids, embedding, created_at, updated_at "
                f"FROM memory_items WHERE id IN ({placeholders})",
                list(touched),
            ).fetchall()
            rows_by_id = {row["id"]: row for row in rows}
        for item_id in touched:
            row = rows_by_id.get(item_id)
            if row is None or row["user_id"] != user_id:
                raise MemoryCurationError(
                    f"Memory Item {item_id} is unavailable"
                )
            snapshot = snapshots_by_id[item_id]
            if (
                row["content"] != snapshot.content
                or row["updated_at"] != snapshot.updated_at
            ):
                raise MemoryCurationConflict(
                    f"Memory Item {item_id} changed after packaging"
                )
        if requested_evidence:
            placeholders = ",".join("?" * len(requested_evidence))
            rows = conn.execute(
                f"SELECT id, user_id, role FROM messages "
                f"WHERE id IN ({placeholders})",
                list(requested_evidence),
            ).fetchall()
            valid = {
                row["id"] for row in rows
                if row["user_id"] == user_id and row["role"] == "user"
            }
            if valid != requested_evidence:
                raise MemoryCurationError(
                    "all evidence must be same-user user messages"
                )

        evidence_by_id = {
            item_id: decode_evidence_message_ids(row["evidence_message_ids"])
            for item_id, row in rows_by_id.items()
        }
        now = datetime.now(config.TZ).isoformat()
        created: list[int] = []
        changed: list[int] = []
        archived: list[int] = []
        for operation in parsed:
            evidence = operation["evidence"]
            if operation["op"] == "create":
                if not evidence:
                    raise MemoryCurationError("create requires evidence")
                item_id = insert_memory_item(
                    user_id,
                    operation["content"],
                    operation["importance"],
                    source="",
                    tags=operation.get("tags"),
                    evidence_message_ids=evidence,
                    conn=conn,
                )
                created.append(item_id)
                continue

            if operation["op"] == "edit":
                item_id = operation["item_id"]
                row = rows_by_id[item_id]
                existing_evidence = set(evidence_by_id[item_id])
                content_changed = operation["content"] != row["content"]
                if content_changed and not (set(evidence) - existing_evidence):
                    raise MemoryCurationError(
                        "content-changing edit requires new evidence"
                    )
                _insert_memory_trash_snapshot(
                    conn, row, deleted_by="weekly_edit", deleted_at=now,
                )
                evidence_json = encode_evidence_message_ids(
                    merge_evidence_message_ids(evidence_by_id[item_id], evidence)
                )
                embedding = None if content_changed else row["embedding"]
                conn.execute(
                    "UPDATE memory_items SET content = ?, importance = ?, "
                    "evidence_message_ids = ?, updated_at = ?, "
                    "embedding = ? WHERE id = ? AND user_id = ?",
                    (
                        operation["content"], operation["importance"],
                        evidence_json, now, embedding,
                        item_id, user_id,
                    ),
                )
                if content_changed:
                    _invalidate_memory_kg_indexes(conn, [item_id])
                    _sync_memory_item_indexes(
                        conn, item_id, operation["content"], None,
                    )
                changed.append(item_id)
                continue

            if operation["op"] == "merge":
                keep_id = operation["keep_item_id"]
                remove_ids = list(operation["remove_item_ids"])
                all_ids = [keep_id, *remove_ids]
                existing_evidence = {
                    evidence_id
                    for item_id in all_ids
                    for evidence_id in evidence_by_id[item_id]
                }
                source_contents = {rows_by_id[item_id]["content"] for item_id in all_ids}
                if (
                    operation["content"] not in source_contents
                    and not (set(evidence) - existing_evidence)
                ):
                    raise MemoryCurationError(
                        "generated merge content requires new evidence"
                    )
                for item_id in all_ids:
                    _insert_memory_trash_snapshot(
                        conn,
                        rows_by_id[item_id],
                        deleted_by=(
                            "weekly_merge_keep"
                            if item_id == keep_id
                            else "weekly_merge_remove"
                        ),
                        deleted_at=now,
                    )
                evidence_json = encode_evidence_message_ids(
                    merge_evidence_message_ids(
                        *(evidence_by_id[item_id] for item_id in all_ids),
                        evidence,
                    )
                )
                conn.execute(
                    "UPDATE memory_items SET content = ?, importance = ?, "
                    "evidence_message_ids = ?, updated_at = ?, "
                    "embedding = NULL WHERE id = ? AND user_id = ?",
                    (
                        operation["content"], operation["importance"],
                        evidence_json, now,
                        keep_id, user_id,
                    ),
                )
                _invalidate_memory_kg_indexes(conn, all_ids)
                _sync_memory_item_indexes(
                    conn, keep_id, operation["content"], None,
                )
                _delete_memory_item_indexes(conn, remove_ids)
                placeholders = ",".join("?" * len(remove_ids))
                conn.execute(
                    f"DELETE FROM memory_items WHERE user_id = ? "
                    f"AND id IN ({placeholders})",
                    [user_id, *remove_ids],
                )
                changed.append(keep_id)
                archived.extend(remove_ids)
                continue

            item_id = operation["item_id"]
            row = rows_by_id[item_id]
            existing_evidence = set(evidence_by_id[item_id])
            if not (set(evidence) - existing_evidence):
                raise MemoryCurationError(
                    "archive requires new invalidation evidence"
                )
            evidence_json = encode_evidence_message_ids(
                merge_evidence_message_ids(evidence_by_id[item_id], evidence)
            )
            _insert_memory_trash_snapshot(
                conn,
                row,
                deleted_by="weekly_archive",
                deleted_at=now,
                evidence_message_ids=evidence_json,
            )
            _invalidate_memory_kg_indexes(conn, [item_id])
            _delete_memory_item_indexes(conn, [item_id])
            conn.execute(
                "DELETE FROM memory_items WHERE id = ? AND user_id = ?",
                (item_id, user_id),
            )
            archived.append(item_id)

        result = MemoryCurationResult(
            created_ids=tuple(created),
            changed_ids=tuple(changed),
            archived_ids=tuple(archived),
        )
        conn.execute(
            "INSERT INTO weekly_curation_batches "
            "(user_id, period_key, result_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                user_id,
                period_key,
                json.dumps(_result_payload(result), separators=(",", ":")),
                now,
            ),
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
