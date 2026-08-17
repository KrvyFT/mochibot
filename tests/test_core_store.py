import hashlib
import json

import pytest

import mochi.core_store as core_store


def test_legacy_identity_migrates_once_with_backups():
    from mochi.db import _connect

    prompts = core_store.DATA_DIR / "prompts" / "system_chat"
    prompts.mkdir(parents=True)
    raw_soul = b"Custom soul\r\n"
    (prompts / "soul.md").write_bytes(raw_soul)
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
        "Custom soul", "Legacy relationship",
    ))
    backup = core_store.DATA_DIR / status["backup"]["directory"]
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"]["soul_override"]["sha256"] == hashlib.sha256(
        raw_soul,
    ).hexdigest()
    assert core_store.initialize_core(1)["target_sha256"] == status["target_sha256"]
def test_agent_replaces_complete_core_with_internal_conflict_check():
    core_store.replace_core("alpha\n\nbeta")
    expected = core_store.read_core()

    core_store.replace_core_exact(
        expected_content=expected,
        content="gamma\n\nbeta",
    )
    assert core_store.read_core() == "gamma\n\nbeta"
    assert list((core_store.DATA_DIR / "core_history").glob("*.md"))

    core_store.replace_core("admin changed it")
    with pytest.raises(core_store.CoreConflictError):
        core_store.replace_core_exact(
            expected_content="gamma\n\nbeta",
            content="agent overwrote it",
        )
    assert core_store.read_core() == "admin changed it"
