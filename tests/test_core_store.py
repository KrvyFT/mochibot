import hashlib
import json

import pytest

import mochi.core_store as core_store


def test_legacy_identity_and_notes_migrate_once_with_backups():
    from mochi.db import _connect

    prompts = core_store.DATA_DIR / "prompts" / "system_chat"
    prompts.mkdir(parents=True)
    raw_soul = b"Custom soul\r\n"
    (prompts / "soul.md").write_bytes(raw_soul)
    notes = core_store.DATA_DIR / "notes.md"
    notes.write_text("- follow up next week\n", encoding="utf-8")
    conn = _connect()
    conn.execute(
        "CREATE TABLE core_memory "
        "(user_id INTEGER PRIMARY KEY, content TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO core_memory VALUES (1, 'Legacy relationship', 'now')"
    )
    conn.commit()
    conn.close()

    status = core_store.initialize_core(1)
    content = core_store.read_core()

    assert status["status"] == "migrated"
    assert all(text in content for text in (
        "Custom soul", "Legacy relationship", "follow up next week",
    ))
    assert not notes.exists()
    backup = core_store.DATA_DIR / status["backup"]["directory"]
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"]["soul_override"]["sha256"] == hashlib.sha256(
        raw_soul,
    ).hexdigest()
    assert core_store.initialize_core(1)["target_sha256"] == status["target_sha256"]


def test_fresh_core_uses_bundled_identity():
    status = core_store.initialize_core(1)
    content = core_store.read_core()
    assert status["status"] == "fresh"
    assert "# \u6211" in content
    assert "AI \u966a\u4f34\u642d\u5b50" in content


def test_exact_patch_conflict_and_snapshot_restore(monkeypatch):
    import mochi.config as config

    core_store.replace_core("alpha\n\nbeta")
    with pytest.raises(core_store.CoreConflictError):
        core_store.update_core(
            action="edit", old_text="missing", new_text="changed",
        )
    assert core_store.read_core() == "alpha\n\nbeta"

    core_store.update_core(
        action="edit", old_text="alpha", new_text="gamma",
    )
    core_store.replace_core("short")
    snapshot_id = core_store.list_core_snapshots()[0]["id"]
    monkeypatch.setattr(config, "CORE_MAX_TOKENS", 1)
    restored = core_store.restore_core_snapshot(snapshot_id)
    assert restored["changed"] is True
    assert "gamma" in core_store.read_core()
