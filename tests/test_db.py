"""High-value SQLite regression tests.

These tests intentionally cover durable user data and upgrade boundaries, not
every query helper.
"""

import sqlite3

from mochi.db import (
    delete_memory_items,
    finish_tool_execution,
    get_conversation_context,
    get_recent_tool_executions,
    init_db,
    list_memory_trash,
    recall_memory,
    restore_memory_from_trash,
    save_memory_item,
    save_message,
    start_tool_execution,
)


def test_context_uses_exact_current_message_and_never_revives_stale_orphans():
    save_message(1, "user", "Can you see images?", turn_id="stale-question")
    save_message(
        1,
        "assistant",
        "No, I cannot see images.",
        turn_id="attention:answered-elsewhere",
        processed=True,
    )
    save_message(
        1,
        "assistant",
        "I was thinking about dinner.",
        turn_id="free_time:preface",
        processed=True,
    )
    current_id = save_message(
        1,
        "user",
        "What did you mean just now?",
        turn_id="current-turn",
    )
    save_message(
        1,
        "assistant",
        "A concurrent proactive message.",
        turn_id="attention:concurrent",
        processed=True,
    )

    context = get_conversation_context(
        1,
        recent_turns=2,
        current_user_message_id=current_id,
    )

    visible = context["recent"] + context["trailing"]
    assert [item["content"] for item in visible] == [
        "No, I cannot see images.",
        "I was thinking about dinner.",
        "What did you mean just now?",
    ]
    assert get_conversation_context(1)["trailing"] == []


def test_tool_ledger_keeps_real_receipt_and_filters_non_changes():
    success_id = start_tool_execution(
        turn_id="turn_1", tool_call_id="call_1", user_id=1,
        source="chat", skill_name="reminder",
        tool_name="manage_reminder", action="create",
        arguments_json='{"message":"report"}',
    )
    finish_tool_execution(
        success_id, status="success", result_summary="Reminder #27 set",
        entity_refs=["reminder:27"], state_changed=True,
    )
    failed_id = start_tool_execution(
        turn_id="turn_2", tool_call_id="call_2", user_id=1,
        source="chat", skill_name="reminder",
        tool_name="manage_reminder", action="create", arguments_json="{}",
    )
    finish_tool_execution(failed_id, status="failed", result_summary="failed")

    rows = get_recent_tool_executions(1, state_changes_only=True)

    assert len(rows) == 1
    assert rows[0]["arguments"] == {"message": "report"}
    assert rows[0]["entity_refs"] == ["reminder:27"]


def test_deleted_memory_can_be_restored():
    first_event = save_memory_item(1, "[2026-08-15] Started a new project")
    second_event = save_memory_item(1, "[2026-08-15] Started learning Japanese")
    assert first_event != second_event

    memory_id = save_memory_item(1, "Likes jasmine tea")
    assert delete_memory_items([memory_id], deleted_by="user") == 1
    assert recall_memory(1, query="jasmine") == []

    trash = list_memory_trash(1)
    assert trash[0]["content"] == "Likes jasmine tea"
    assert restore_memory_from_trash(trash[0]["id"], 1) is not None
    assert recall_memory(1, query="jasmine")[0]["content"] == "Likes jasmine tea"


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
        "source TEXT, processed INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT)"
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
    assert {"processed", "image_data", "tool_history", "turn_id"} <= message_columns
    assert {"embedding", "access_count", "last_accessed"} <= memory_columns
    assert message == "keep me"
    assert memory == "keep this memory"
