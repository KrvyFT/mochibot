"""Single-owner file storage for Mochi's always-on Core context."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from mochi.token_estimator import estimate_tokens

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LEGACY_PROMPTS_DIR = (
    Path(__file__).resolve().parent / "prompts" / "migration_legacy"
)
CORE_FILENAME = "core.md"
HISTORY_DIRNAME = "core_history"
MIGRATION_BACKUP_DIRNAME = "core_migration_backup"
MIGRATION_STATUS_FILENAME = "core_migration.json"
NOTES_RETIREMENT_BACKUP_DIRNAME = "notes_retirement_backup"
NOTES_RETIREMENT_STATUS_FILENAME = "notes_retirement.json"
WEEKLY_RECEIPTS_DIRNAME = "core_weekly_receipts"
CORE_LOCK_FILENAME = ".core.lock"
CORE_LOCK_TIMEOUT_SECONDS = 10.0
CORE_LOCK_POLL_SECONDS = 0.05
SNAPSHOT_LIMIT = 10
MIN_DUPLICATE_LIST_ITEM_LENGTH = 8
LEGACY_ADD_RETIRED_MESSAGE = (
    "Core action 'add' is retired because blind append creates duplicate content. "
    "Use edit to revise existing text or insert_after with an exact unique anchor_text."
)

_lock = threading.RLock()
log = logging.getLogger(__name__)


class CoreError(ValueError):
    """Base error for rejected Core operations."""


class CoreConflictError(CoreError):
    """Raised when an exact edit/delete target is absent or not unique."""


class CoreLimitError(CoreError):
    """Raised when a write exceeds the configured Core token budget."""


class CoreHygieneError(CoreError):
    """Raised when a write would create exact duplicate document content."""


class CoreLockTimeout(CoreError):
    """Raised when another process holds the Core transaction lock too long."""


def _core_path() -> Path:
    return DATA_DIR / CORE_FILENAME


def _core_lock_path() -> Path:
    return DATA_DIR / CORE_LOCK_FILENAME


def _history_dir() -> Path:
    return DATA_DIR / HISTORY_DIRNAME


def _migration_status_path() -> Path:
    return DATA_DIR / MIGRATION_STATUS_FILENAME


def _notes_retirement_status_path() -> Path:
    return DATA_DIR / NOTES_RETIREMENT_STATUS_FILENAME


def _weekly_receipt_path(user_id: int, period_key: str) -> Path:
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise CoreError("Weekly Core receipt user_id must be an integer.")
    if not isinstance(period_key, str) or not period_key.strip():
        raise CoreError("Weekly Core receipt period_key is required.")
    digest = hashlib.sha256(period_key.encode("utf-8")).hexdigest()[:16]
    return DATA_DIR / WEEKLY_RECEIPTS_DIRNAME / f"{user_id}--{digest}.json"


def _max_tokens() -> int:
    from mochi.config import CORE_MAX_TOKENS

    return CORE_MAX_TOKENS


def _serialize(content: str) -> bytes:
    """Return the exact bytes persisted by Core storage."""
    text = content
    if text and not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def _sha256(content: str) -> str:
    return hashlib.sha256(_serialize(content)).hexdigest()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _stats(content: str) -> dict:
    return {
        "chars": len(content),
        "tokens": estimate_tokens(content),
        "max_tokens": _max_tokens(),
    }


def get_core_stats() -> dict:
    return _stats(read_core())


def get_core_hygiene_status() -> dict:
    with _transaction():
        _initialize_core_unlocked()
        content = _core_path().read_text(encoding="utf-8").strip()
        issue_codes = list(dict.fromkeys(
            issue["code"] for issue in _hygiene_issues(content)
        ))
        return {
            "needs_cleanup": bool(issue_codes),
            "cleanup_issues": issue_codes,
        }


def _normalize_document_text(content: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", content).strip(),
    ).casefold()


def _hygiene_issues(content: str) -> list[dict]:
    """Return privacy-safe exact duplicate findings for free-text Core."""
    issues: list[dict] = []
    headings: dict[str, int] = {}
    paragraphs: dict[str, tuple[int, int]] = {}
    list_items: dict[str, int] = {}
    paragraph_lines: list[str] = []
    paragraph_start = 0

    def finish_paragraph(end_line: int) -> None:
        nonlocal paragraph_lines, paragraph_start
        if not paragraph_lines:
            return
        key = _normalize_document_text("\n".join(paragraph_lines))
        if key:
            previous = paragraphs.get(key)
            if previous:
                issues.append({
                    "code": "duplicate_paragraph",
                    "first_lines": previous,
                    "duplicate_lines": (paragraph_start, end_line),
                })
            else:
                paragraphs[key] = (paragraph_start, end_line)
        paragraph_lines = []
        paragraph_start = 0

    lines = content.splitlines()
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        heading_match = re.match(r"^#(?!#)[ \t]+(.+?)\s*$", line)
        any_heading = re.match(r"^\s*#{1,6}(?:[ \t]+|$)", line)
        list_match = re.match(
            r"^\s*(?:[-+*]|\d+[.)])[ \t]+(.+?)\s*$",
            line,
        )

        if not stripped or any_heading or list_match:
            finish_paragraph(line_number - 1)

        if heading_match:
            key = _normalize_document_text(heading_match.group(1))
            previous = headings.get(key)
            if key and previous:
                issues.append({
                    "code": "duplicate_h1",
                    "first_line": previous,
                    "duplicate_line": line_number,
                })
            elif key:
                headings[key] = line_number

        if list_match:
            key = _normalize_document_text(list_match.group(1))
            if len(key) >= MIN_DUPLICATE_LIST_ITEM_LENGTH:
                previous = list_items.get(key)
                if previous:
                    issues.append({
                        "code": "duplicate_list_item",
                        "first_line": previous,
                        "duplicate_line": line_number,
                    })
                else:
                    list_items[key] = line_number

        if stripped and not any_heading and not list_match:
            if not paragraph_lines:
                paragraph_start = line_number
            paragraph_lines.append(line)

    finish_paragraph(len(lines))
    return issues


def _hygiene_error_message(issue: dict) -> str:
    code = issue["code"]
    if code == "duplicate_h1":
        location = f"lines {issue['first_line']} and {issue['duplicate_line']}"
        fix = "Merge, rename, or remove one H1 heading"
    elif code == "duplicate_paragraph":
        first_start, first_end = issue["first_lines"]
        duplicate_start, duplicate_end = issue["duplicate_lines"]
        location = (
            f"lines {first_start}-{first_end} and "
            f"{duplicate_start}-{duplicate_end}"
        )
        fix = "Merge, rewrite, or remove one exact paragraph"
    else:
        location = f"lines {issue['first_line']} and {issue['duplicate_line']}"
        fix = "Merge, rewrite, or remove one exact list item"
    return (
        f"Core hygiene conflict [{code}] at {location}. {fix}, then retry "
        "with edit or insert_after. No content was written."
    )


def _validate_budget(content: str) -> str:
    normalized = content.strip()
    tokens = estimate_tokens(normalized)
    if tokens > _max_tokens():
        raise CoreLimitError(
            f"Core is about {tokens} tokens; limit is {_max_tokens()}. "
            "Shorten it before saving."
        )
    return normalized


def _validate(content: str) -> str:
    normalized = _validate_budget(content)
    issues = _hygiene_issues(normalized)
    if issues:
        raise CoreHygieneError(_hygiene_error_message(issues[0]))
    return normalized


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _atomic_write(path: Path, content: str) -> None:
    _atomic_write_bytes(path, _serialize(content))


@contextmanager
def _interprocess_lock(timeout: float | None = None):
    """Hold a cross-platform advisory lock for one complete Core transaction."""
    timeout = CORE_LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    path = _core_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")

    deadline = time.monotonic() + max(0.0, timeout)
    acquired = False
    try:
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise CoreLockTimeout(
                        f"Timed out after {timeout:.1f}s waiting for Core lock: {path}"
                    ) from exc
                time.sleep(CORE_LOCK_POLL_SECONDS)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def _transaction():
    with _lock:
        with _interprocess_lock():
            yield


def _safe_source(source: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", source.strip()).strip("-")
    return cleaned or "unknown"


def _snapshot_unlocked(content: str, source: str) -> dict | None:
    if not content and not _core_path().exists():
        return None
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    source_slug = _safe_source(source)
    digest = _sha256(content)[:12]
    snapshot_id = f"{timestamp}--{source_slug}--{digest}.md"
    path = _history_dir() / snapshot_id
    _atomic_write(path, content)
    snapshots = sorted(_history_dir().glob("*.md"), reverse=True)
    for stale in snapshots[SNAPSHOT_LIMIT:]:
        stale.unlink()
    return {
        "id": snapshot_id,
        "created_at": now.isoformat(),
        "source": source,
        "chars": len(content),
        "tokens": estimate_tokens(content),
    }


def list_core_snapshots() -> list[dict]:
    with _transaction():
        results = []
        for path in sorted(_history_dir().glob("*.md"), reverse=True):
            stem = path.stem
            parts = stem.split("--", 2)
            if len(parts) != 3:
                continue
            timestamp, source, digest = parts
            try:
                created = datetime.strptime(
                    timestamp, "%Y%m%dT%H%M%S%fZ"
                ).replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                created = timestamp
            content = path.read_text(encoding="utf-8").rstrip("\n")
            results.append(
                {
                    "id": path.name,
                    "created_at": created,
                    "source": source,
                    "hash": digest,
                    "chars": len(content),
                    "tokens": estimate_tokens(content),
                }
            )
        return results[:SNAPSHOT_LIMIT]


def read_core() -> str:
    with _transaction():
        _ensure_core_ready_unlocked()
        return _core_path().read_text(encoding="utf-8").strip()


def replace_core(content: str, *, source: str = "admin") -> dict:
    with _transaction():
        _ensure_core_ready_unlocked()
        normalized = _validate(content)
        current = _core_path().read_text(encoding="utf-8").strip()
        if current == normalized:
            return {"changed": False, **_stats(current)}
        _snapshot_unlocked(current, source)
        _atomic_write(_core_path(), normalized)
        return {"changed": True, **_stats(normalized)}


def _unique_replace(content: str, old_text: str, new_text: str) -> str:
    if not old_text:
        raise CoreConflictError("old_text is required.")
    count = content.count(old_text)
    if count != 1:
        raise CoreConflictError(
            f"old_text must match exactly once; found {count} matches."
        )
    return content.replace(old_text, new_text, 1)


def _unique_insert_after(content: str, anchor_text: str, addition: str) -> str:
    if not anchor_text:
        raise CoreConflictError("anchor_text is required for insert_after.")
    inserted = addition.strip()
    if not inserted:
        raise CoreError("content is required for insert_after.")
    count = content.count(anchor_text)
    if count != 1:
        raise CoreConflictError(
            f"anchor_text must match exactly once; found {count} matches. "
            "Read Core again with view_core_memory and retry with an exact unique anchor."
        )
    insert_at = content.index(anchor_text) + len(anchor_text)
    before = content[:insert_at]
    after = content[insert_at:]
    before_separator = "" if before.endswith(("\n", "\r")) else "\n\n"
    after_separator = "" if not after or after.startswith(("\n", "\r")) else "\n\n"
    return f"{before}{before_separator}{inserted}{after_separator}{after}"


def _apply_operation(content: str, operation: dict) -> str:
    if not isinstance(operation, dict):
        raise CoreError("Each batch operation must be an object.")
    action = str(operation.get("action") or "").strip().lower()
    if action == "add":
        raise CoreError(LEGACY_ADD_RETIRED_MESSAGE)
    if action == "edit":
        old_text = str(operation.get("old_text") or "")
        new_text = str(operation.get("new_text") or "")
        if not new_text:
            raise CoreError("new_text is required for edit.")
        return _unique_replace(content, old_text, new_text)
    if action == "delete":
        old_text = str(operation.get("old_text") or "")
        return _unique_replace(content, old_text, "")
    if action == "insert_after":
        return _unique_insert_after(
            content,
            str(operation.get("anchor_text") or ""),
            str(operation.get("content") or ""),
        )
    raise CoreError(
        f"Unknown Core action: {action or '(empty)'}. "
        "Use edit, delete, insert_after, or batch."
    )


def update_core(
    *,
    action: str,
    content: str = "",
    old_text: str = "",
    new_text: str = "",
    anchor_text: str = "",
    operations: Iterable[dict] | None = None,
    source: str = "main",
) -> dict:
    with _transaction():
        _ensure_core_ready_unlocked()
        current = _core_path().read_text(encoding="utf-8").strip()
        action = str(action or "").strip().lower()
        if action == "batch":
            ops = list(operations or [])
            if not ops:
                raise CoreError("operations is required for batch.")
        else:
            ops = [
                {
                    "action": action,
                    "content": content,
                    "old_text": old_text,
                    "new_text": new_text,
                    "anchor_text": anchor_text,
                }
            ]

        updated = current
        for operation in ops:
            updated = _apply_operation(updated, operation)
        updated = _validate(updated)
        if updated == current:
            return {"changed": False, **_stats(current)}
        _snapshot_unlocked(current, source)
        _atomic_write(_core_path(), updated)
        return {"changed": True, **_stats(updated)}


def _read_weekly_receipt_unlocked(user_id: int, period_key: str) -> dict:
    path = _weekly_receipt_path(user_id, period_key)
    if not path.is_file():
        return {}
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return receipt if receipt.get("period_key") == period_key else {}


def has_weekly_core_update(user_id: int, period_key: str) -> bool:
    with _transaction():
        return bool(_read_weekly_receipt_unlocked(user_id, period_key))


def update_weekly_core_exact(
    *,
    user_id: int,
    period_key: str,
    expected_content: str,
    operations: Iterable[dict],
) -> str:
    """Apply one receipt-backed batch to an exact visible Core snapshot.

    The semantic choice of operations belongs to Main. This function only
    applies deterministic exact patches and records their resulting document
    hash so a retried Weekly run cannot replay a different change.
    """
    if not isinstance(expected_content, str):
        raise CoreError("expected_content must be text.")
    ops = list(operations)
    if not ops:
        raise CoreError("operations is required for Weekly Core update.")

    updated = expected_content
    for operation in ops:
        updated = _apply_operation(updated, operation)
    updated = _validate(updated)
    requested_hash = _sha256(updated)

    with _transaction():
        _ensure_core_ready_unlocked(user_id)
        receipt = _read_weekly_receipt_unlocked(user_id, period_key)
        if receipt:
            return (
                "replayed"
                if receipt.get("content_sha256") == requested_hash
                else "conflict"
            )

        current = _core_path().read_text(encoding="utf-8").strip()
        if current != expected_content:
            # A process may have completed the durable Core write just before
            # persisting its receipt. Recover only an identical deterministic
            # target; all other concurrent edits remain conflicts.
            if current != updated:
                return "conflict"
            outcome = "replayed"
            snapshot = None
        elif current == updated:
            outcome = "committed"
            snapshot = None
        else:
            snapshot = _snapshot_unlocked(current, f"weekly-{period_key}")
            _atomic_write(_core_path(), updated)
            outcome = "committed"

        receipt = {
            "user_id": user_id,
            "period_key": period_key,
            "content_sha256": requested_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "snapshot": snapshot,
        }
        _atomic_write(
            _weekly_receipt_path(user_id, period_key),
            json.dumps(receipt, ensure_ascii=False, indent=2),
        )
        return outcome


def restore_core_snapshot(snapshot_id: str, *, source: str = "admin-restore") -> dict:
    if Path(snapshot_id).name != snapshot_id:
        raise CoreError("Invalid snapshot id.")
    with _transaction():
        _ensure_core_ready_unlocked()
        history_root = _history_dir().resolve()
        path = (_history_dir() / snapshot_id).resolve()
        if path.parent != history_root:
            raise CoreError("Invalid snapshot id.")
        if not path.is_file():
            raise CoreError("Core snapshot not found.")
        restored = path.read_text(encoding="utf-8").strip()
        cleanup_issues = list(dict.fromkeys(
            issue["code"] for issue in _hygiene_issues(restored)
        ))
        current = _core_path().read_text(encoding="utf-8").strip()
        if restored == current:
            return {
                "changed": False,
                "needs_cleanup": bool(cleanup_issues),
                "cleanup_issues": cleanup_issues,
                "over_budget": estimate_tokens(restored) > _max_tokens(),
                **_stats(current),
            }
        _snapshot_unlocked(current, source)
        _atomic_write(_core_path(), restored)
        return {
            "changed": True,
            "needs_cleanup": bool(cleanup_issues),
            "cleanup_issues": cleanup_issues,
            "over_budget": estimate_tokens(restored) > _max_tokens(),
            **_stats(restored),
        }


def _read_optional(path: Path) -> tuple[bool, str, bytes]:
    if not path.is_file():
        return False, "", b""
    raw = path.read_bytes()
    return True, raw.decode("utf-8").strip(), raw


def _has_body(content: str) -> bool:
    return any(line.strip() and not line.lstrip().startswith("#") for line in content.splitlines())


def _demote_headings(content: str) -> str:
    return re.sub(r"(?m)^#(?!#)", "##", content.strip())


def _compose_legacy_core(user_id: int) -> tuple[str, dict[str, bytes], bool]:
    sources: dict[str, bytes] = {}
    override_root = DATA_DIR / "prompts" / "system_chat"
    prompt_parts: dict[str, str] = {}
    for name in ("soul", "user", "tone"):
        override_exists, override, raw = _read_optional(
            override_root / f"{name}.md"
        )
        _, default, _ = _read_optional(
            LEGACY_PROMPTS_DIR / f"{name}.md"
        )
        prompt_parts[name] = override if override_exists else default
        if override_exists:
            sources[f"{name}_override"] = raw

    legacy_db_core = ""
    from mochi.db import _connect, get_core_memory

    legacy_db_core = get_core_memory(user_id) or ""
    if not legacy_db_core:
        conn = _connect()
        try:
            try:
                row = conn.execute(
                    "SELECT content FROM core_memory "
                    "WHERE TRIM(content) != '' ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
            except sqlite3.OperationalError:
                row = None
            legacy_db_core = row["content"] if row else ""
        finally:
            conn.close()
    if legacy_db_core.strip():
        sources["sqlite_core_memory"] = legacy_db_core.encode("utf-8")

    sections = []
    self_parts = [
        part
        for part in (prompt_parts["soul"], prompt_parts["tone"])
        if _has_body(part)
    ]
    if self_parts:
        sections.append(
            "# 我\n\n"
            + "\n\n".join(_demote_headings(part) for part in self_parts)
        )
    if _has_body(prompt_parts["user"]):
        sections.append(
            "# 用户\n\n" + _demote_headings(prompt_parts["user"])
        )
    if _has_body(legacy_db_core):
        sections.append("# 我们\n\n" + _demote_headings(legacy_db_core))
    return "\n\n".join(sections).strip(), sources, bool(sources)


def _write_migration_backup(sources: dict[str, bytes], target: str) -> dict:
    now = datetime.now(timezone.utc)
    backup_dir = (
        DATA_DIR
        / MIGRATION_BACKUP_DIRNAME
        / now.strftime("%Y%m%dT%H%M%S%fZ")
    )
    backup_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "created_at": now.isoformat(),
        "sources": {},
        "target": {"path": CORE_FILENAME, "sha256": _sha256(target)},
    }
    for name, content in sources.items():
        filename = f"{_safe_source(name)}.md"
        _atomic_write_bytes(backup_dir / filename, content)
        manifest["sources"][name] = {
            "path": filename,
            "bytes": len(content),
            "sha256": _sha256_bytes(content),
        }
    _atomic_write(
        backup_dir / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )
    return {"directory": str(backup_dir.relative_to(DATA_DIR)), **manifest}


def _initialize_core_unlocked(user_id: int | None = None) -> dict:
    if _core_path().is_file():
        return _read_migration_status_unlocked() or {
            "status": "existing",
            "target": CORE_FILENAME,
        }

    if user_id is None:
        try:
            from mochi.config import OWNER_USER_ID

            user_id = OWNER_USER_ID or 0
        except Exception:
            user_id = 0

    content, sources, migrated = _compose_legacy_core(user_id)
    content = content.strip()
    backup = _write_migration_backup(sources, content) if migrated else None
    stats = _stats(content)
    over_budget = stats["tokens"] > stats["max_tokens"]
    if migrated:
        if not over_budget:
            content = _validate_budget(content)
    else:
        content = _validate(content)
    cleanup_issues = list(dict.fromkeys(
        issue["code"] for issue in _hygiene_issues(content)
    ))
    _atomic_write(_core_path(), content)
    status = {
        "status": (
            "migrated_over_budget"
            if migrated and over_budget
            else "migrated" if migrated else "fresh"
        ),
        "target": CORE_FILENAME,
        "target_sha256": _sha256(content),
        "source_names": sorted(sources),
        "backup": backup,
        "stats": stats,
        "over_budget": over_budget,
        "needs_cleanup": bool(cleanup_issues),
        "cleanup_issues": cleanup_issues,
    }
    _atomic_write(
        _migration_status_path(),
        json.dumps(status, ensure_ascii=False, indent=2),
    )
    return status


def _ensure_core_ready_unlocked(user_id: int | None = None) -> dict:
    status = _initialize_core_unlocked(user_id)
    _retire_notes_unlocked()
    return status


def initialize_core(user_id: int | None = None) -> dict:
    with _transaction():
        return _ensure_core_ready_unlocked(user_id)


def _read_migration_status_unlocked() -> dict:
    path = _migration_status_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def get_core_migration_status() -> dict:
    with _transaction():
        return _read_migration_status_unlocked()


def _normalize_note_entry(content: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", content).strip()).casefold()


def _extract_note_entries(content: str) -> list[str]:
    """Return list items from exact ``## Notes`` sections only."""
    entries: list[str] = []
    in_notes = False
    seen: set[str] = set()
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "## Notes":
            in_notes = True
            continue
        if re.match(r"^#{1,6}(?:\s|$)", stripped):
            in_notes = False
            continue
        if not in_notes:
            continue
        match = re.match(r"^[-*+]\s+(.+?)\s*$", stripped)
        if not match:
            continue
        entry = match.group(1).strip()
        key = _normalize_note_entry(entry)
        if key and key not in seen:
            seen.add(key)
            entries.append(entry)
    return entries


def _core_entry_keys(content: str) -> set[str]:
    keys: set[str] = set()
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", stripped)
        key = _normalize_note_entry(stripped)
        if key:
            keys.add(key)
    return keys


def _merge_notes_into_core(content: str, entries: list[str]) -> tuple[str, list[str]]:
    existing = _core_entry_keys(content)
    additions = [
        entry for entry in entries
        if _normalize_note_entry(entry) not in existing
    ]
    if not additions:
        return content, []

    bullets = [f"- {entry}" for entry in additions]
    prefix = content.rstrip()
    return f"{prefix}\n\n" + "\n".join(bullets) if prefix else "\n".join(bullets), additions


def _read_notes_retirement_status_unlocked() -> dict:
    path = _notes_retirement_status_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_notes_retirement_status_unlocked(status: dict) -> None:
    _atomic_write(
        _notes_retirement_status_path(),
        json.dumps(status, ensure_ascii=False, indent=2),
    )


def _notes_retirement_failure(error: Exception) -> dict:
    status = {
        "status": "failed",
        "error": str(error),
        "over_budget": False,
    }
    try:
        _write_notes_retirement_status_unlocked(status)
    except Exception:
        log.exception("Could not persist Notes retirement failure status")
    log.error("Notes retirement migration failed: %s", error, exc_info=True)
    return status


def _finish_committed_notes_move(
    notes_path: Path,
    source_bytes: bytes,
    core_bytes: bytes,
    status: dict,
) -> dict | None:
    """Finish a move interrupted after the manifest and status were committed."""
    if status.get("status") not in {
        "migrated",
        "migrated_over_budget",
        "retired_no_entries",
    }:
        return None
    source = status.get("source") or {}
    target = status.get("target_after") or {}
    if (
        source.get("bytes") != len(source_bytes)
        or source.get("sha256") != _sha256_bytes(source_bytes)
        or target.get("bytes") != len(core_bytes)
        or target.get("sha256") != _sha256_bytes(core_bytes)
    ):
        return None

    backup_rel = Path(str(status.get("backup") or ""))
    retired_name = str(status.get("retired_source") or "")
    backup_name = str(source.get("backup_path") or "")
    if (
        not backup_rel.parts
        or backup_rel.is_absolute()
        or ".." in backup_rel.parts
        or not retired_name
        or Path(retired_name).name != retired_name
        or not backup_name
        or Path(backup_name).name != backup_name
    ):
        return None
    backup_dir = DATA_DIR / backup_rel
    backup_source = backup_dir / backup_name
    if (
        not backup_source.is_file()
        or backup_source.read_bytes() != source_bytes
    ):
        return None
    _atomic_write(
        backup_dir / "manifest.json",
        json.dumps(status, ensure_ascii=False, indent=2),
    )
    os.replace(notes_path, backup_dir / retired_name)
    log.info("Completed interrupted Notes source retirement from committed manifest")
    return status


def _retire_notes_unlocked() -> dict:
    _initialize_core_unlocked()
    notes_path = DATA_DIR / "notes.md"
    previous_status = _read_notes_retirement_status_unlocked()
    if not notes_path.is_file():
        if previous_status.get("status") in {
            "migrated",
            "migrated_over_budget",
            "retired_no_entries",
        }:
            return previous_status
        status = {
            "status": "not_needed",
            "source": "notes.md",
            "over_budget": False,
        }
        _write_notes_retirement_status_unlocked(status)
        return status

    core_path = _core_path()
    source_bytes = notes_path.read_bytes()
    core_before_bytes = core_path.read_bytes()
    recovered = _finish_committed_notes_move(
        notes_path,
        source_bytes,
        core_before_bytes,
        previous_status,
    )
    if recovered:
        return recovered
    source_sha = _sha256_bytes(source_bytes)
    core_before_sha = _sha256_bytes(core_before_bytes)
    run_id = f"{source_sha[:12]}--{core_before_sha[:12]}"
    backup_dir = DATA_DIR / NOTES_RETIREMENT_BACKUP_DIRNAME / run_id
    backup_dir.mkdir(parents=True, exist_ok=True)
    notes_backup_path = backup_dir / "notes.md"
    core_backup_path = backup_dir / "core.before.md"
    retired_path = backup_dir / "notes.retired.md"
    manifest_path = backup_dir / "manifest.json"

    _atomic_write_bytes(notes_backup_path, source_bytes)
    _atomic_write_bytes(core_backup_path, core_before_bytes)
    manifest = {
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "path": "notes.md",
            "backup_path": "notes.md",
            "bytes": len(source_bytes),
            "sha256": source_sha,
        },
        "target_before": {
            "path": CORE_FILENAME,
            "backup_path": "core.before.md",
            "bytes": len(core_before_bytes),
            "sha256": core_before_sha,
        },
        "over_budget": False,
    }
    _atomic_write(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )

    try:
        source_text = source_bytes.decode("utf-8")
        core_before = core_before_bytes.decode("utf-8")
        entries = _extract_note_entries(source_text)
        updated, migrated_entries = _merge_notes_into_core(core_before, entries)
        target_bytes = _serialize(updated) if migrated_entries else core_before_bytes
        stats = _stats(updated)
        over_budget = stats["tokens"] > stats["max_tokens"]
        snapshot = None
        core_changed = target_bytes != core_before_bytes
        notes_moved = False
        if core_changed:
            snapshot = _snapshot_unlocked(core_before, "notes-retirement")
            _atomic_write(core_path, updated)
        status_name = (
            "migrated_over_budget"
            if migrated_entries and over_budget
            else "migrated" if migrated_entries
            else "retired_no_entries"
        )
        manifest.update({
            "status": status_name,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "backup": str(backup_dir.relative_to(DATA_DIR)),
            "retired_source": "notes.retired.md",
            "entries_found": len(entries),
            "entries_migrated": len(migrated_entries),
            "entries_skipped": len(entries) - len(migrated_entries),
            "target_after": {
                "path": CORE_FILENAME,
                "bytes": len(target_bytes),
                "sha256": _sha256_bytes(target_bytes),
            },
            "snapshot": snapshot,
            "stats": stats,
            "over_budget": over_budget,
        })
        _write_notes_retirement_status_unlocked(manifest)
        _atomic_write(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        os.replace(notes_path, retired_path)
        notes_moved = True
        return manifest
    except Exception as exc:
        rollback_errors: list[str] = []
        if "notes_moved" in locals() and notes_moved and retired_path.exists():
            try:
                os.replace(retired_path, notes_path)
            except Exception as rollback_exc:
                rollback_errors.append(f"notes rollback failed: {rollback_exc}")
        try:
            if core_path.read_bytes() != core_before_bytes:
                _atomic_write_bytes(core_path, core_before_bytes)
        except Exception as rollback_exc:
            rollback_errors.append(f"Core rollback failed: {rollback_exc}")
        manifest.update({
            "status": "failed",
            "error": str(exc),
            "rollback_errors": rollback_errors,
        })
        try:
            _atomic_write(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
        except Exception:
            log.exception("Could not update Notes retirement backup manifest")
        if rollback_errors:
            raise CoreError(
                "Notes retirement failed and rollback could not be guaranteed: "
                + "; ".join(rollback_errors)
            ) from exc
        return _notes_retirement_failure(exc)


def retire_notes_into_core() -> dict:
    """Retire legacy Notes into Core without truncation or LLM rewriting."""
    try:
        with _transaction():
            return _retire_notes_unlocked()
    except CoreLockTimeout as exc:
        log.error("Notes retirement could not acquire the Core lock: %s", exc)
        return {
            "status": "failed",
            "error": str(exc),
            "over_budget": False,
        }
    except CoreError:
        raise
    except Exception as exc:
        return _notes_retirement_failure(exc)


def get_notes_retirement_status() -> dict:
    with _transaction():
        return _read_notes_retirement_status_unlocked()
