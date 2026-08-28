"""Durable database upgrade and execution-ledger contracts."""

import sqlite3

from mochi.db import (
    _connect,
    init_db,
    recover_interrupted_tool_executions,
    start_tool_execution,
)


def test_startup_recovers_interrupted_tool_executions():
    execution_ids = [
        start_tool_execution(
            turn_id=turn_id,
            tool_call_id=call_id,
            user_id=1,
            source=source,
            skill_name=skill,
            tool_name=tool,
            action=action,
            arguments_json="{}",
        )
        for turn_id, call_id, source, skill, tool, action in (
            (
                "turn_chat",
                "call_chat",
                "chat",
                "habit",
                "checkin_habit",
                "checkin",
            ),
            (
                "turn_reminder",
                "call_reminder",
                "runtime:self_reminder",
                "reminder",
                "manage_reminder",
                "create",
            ),
        )
    ]

    assert recover_interrupted_tool_executions() == 2

    conn = _connect()
    rows = {
        row["id"]: dict(row)
        for row in conn.execute(
            "SELECT id, status, result_summary, finished_at "
            "FROM tool_executions ORDER BY id"
        )
    }
    conn.close()
    for execution_id in execution_ids:
        assert rows[execution_id]["status"] == "failed"
        assert rows[execution_id]["result_summary"] == (
            "Interrupted by process restart"
        )
        assert rows[execution_id]["finished_at"]


def test_old_database_upgrades_messages_and_memory_without_data_loss(
    tmp_path,
    monkeypatch,
):
    import mochi.db as db_module

    db_path = tmp_path / "old-messages.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "role TEXT, content TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO messages VALUES (1, 1, 'user', 'keep me', '2025-01-01')"
    )
    conn.execute(
        "CREATE TABLE memory_items (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "category TEXT, content TEXT, importance INTEGER DEFAULT 1, "
        "source TEXT, processed INTEGER DEFAULT 0, created_at TEXT, "
        "updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO memory_items (id, user_id, category, content) "
        "VALUES (1, 1, 'fact', 'keep this memory')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    init_db()

    conn = sqlite3.connect(db_path)
    message_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(messages)")
    }
    memory_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(memory_items)")
    }
    message = conn.execute(
        "SELECT content FROM messages WHERE id = 1"
    ).fetchone()[0]
    memory = conn.execute(
        "SELECT content FROM memory_items WHERE id = 1"
    ).fetchone()[0]
    conn.close()
    assert {"processed", "image_data", "tool_history", "turn_id"} <= (
        message_columns
    )
    assert {"embedding", "access_count", "last_accessed"} <= memory_columns
    assert message == "keep me"
    assert memory == "keep this memory"

    import logging
    import mochi.admin.admin_env as admin_env
    import mochi.config as config_module
    import mochi.credential_crypto as credential_crypto
    from mochi.admin.__main__ import _ensure_admin_token
    from mochi.skill_config_resolver import resolve_skill_config
    from mochi.skills.base import ConfigField

    monkeypatch.setattr(admin_env, "_PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(config_module, "ADMIN_TOKEN", "")
    token = _ensure_admin_token(logging.getLogger("test"))
    assert token
    assert config_module.ADMIN_TOKEN == token
    assert admin_env.read_env_value("ADMIN_TOKEN") == token

    monkeypatch.setattr(credential_crypto, "_fernet_instance", None)
    encrypted = credential_crypto.encrypt_secret("search-secret")
    assert credential_crypto.is_encrypted(encrypted)

    db_module.set_skill_config("web_search", "BAIDU_API_KEY", encrypted)
    stored = db_module.get_skill_config("web_search")["BAIDU_API_KEY"]
    assert stored != "search-secret"
    assert resolve_skill_config(
        "web_search",
        [ConfigField(
            key="BAIDU_API_KEY",
            type="str",
            default="",
            secret=True,
        )],
    )["BAIDU_API_KEY"] == "search-secret"

    from mochi.skills.skill_management.handler import SkillManagementSkill

    blocked = SkillManagementSkill()._set_skill_config(
        "web_search",
        "BAIDU_API_KEY",
        "",
    )
    assert not blocked.success
    assert db_module.get_skill_config("web_search")["BAIDU_API_KEY"] == stored
