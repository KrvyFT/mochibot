"""High-value SQLite regression tests.

These tests intentionally cover durable user data and upgrade boundaries, not
every query helper.
"""

import asyncio
import hashlib
import json
import sqlite3

import pytest

from mochi.db import (
    _connect,
    delete_memory_items,
    finish_tool_execution,
    get_conversation_context,
    get_recent_tool_executions,
    init_db,
    list_all_memories,
    list_memory_trash,
    recall_memory,
    recover_interrupted_tool_executions,
    restore_memory_from_trash,
    save_memory_item,
    save_message,
    start_tool_execution,
)
from mochi.skills.base import SkillContext
from mochi.skills.habit.handler import HabitSkill
from mochi.skills.memory.handler import MemorySkill


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


def test_startup_recovers_only_interrupted_chat_tool_executions():
    chat_id = start_tool_execution(
        turn_id="turn_chat",
        tool_call_id="call_chat",
        user_id=1,
        source="chat",
        skill_name="habit",
        tool_name="checkin_habit",
        action="checkin",
        arguments_json="{}",
    )
    runtime_id = start_tool_execution(
        turn_id="turn_reminder",
        tool_call_id="call_reminder",
        user_id=1,
        source="runtime:self_reminder",
        skill_name="reminder",
        tool_name="manage_reminder",
        action="create",
        arguments_json="{}",
    )

    assert recover_interrupted_tool_executions() == 1

    conn = _connect()
    rows = {
        row["id"]: dict(row)
        for row in conn.execute(
            "SELECT id, status, result_summary, finished_at "
            "FROM tool_executions ORDER BY id"
        )
    }
    conn.close()
    assert rows[chat_id]["status"] == "failed"
    assert rows[chat_id]["result_summary"] == "Interrupted by process restart"
    assert rows[chat_id]["finished_at"]
    assert rows[runtime_id]["status"] == "running"
    assert rows[runtime_id]["finished_at"] is None


@pytest.mark.asyncio
async def test_habit_threshold_name_resolution_and_reactivation():
    skill = HabitSkill()

    async def execute(tool_name: str, **args):
        return await skill.execute(SkillContext(
            trigger="tool_call",
            user_id=1,
            tool_name=tool_name,
            args=args,
        ))

    created = await execute(
        "edit_habit",
        action="add",
        name="Drink water",
        cycle="daily",
        target=2,
    )
    assert created.success
    conn = _connect()
    habit_id = conn.execute(
        "SELECT id FROM habits WHERE user_id = 1 AND name = 'Drink water'"
    ).fetchone()["id"]
    conn.close()

    checked = await execute(
        "checkin_habit",
        action="checkin",
        habit_name="Drink water",
        count=3,
    )
    assert checked.success
    assert "(3/2)" in checked.output
    conn = _connect()
    assert conn.execute(
        "SELECT COUNT(*) FROM habit_logs WHERE habit_id = ?",
        (habit_id,),
    ).fetchone()[0] == 3
    conn.close()

    updated = await execute(
        "edit_habit",
        action="update",
        habit_name="Drink water",
        context="after meals",
    )
    assert updated.success
    missing = await execute(
        "checkin_habit",
        action="checkin",
        habit_name="Water",
    )
    assert not missing.success
    assert f"#{habit_id} Drink water" in missing.output

    removed = await execute(
        "edit_habit",
        action="remove",
        habit_name="Drink water",
    )
    assert removed.success
    revived = await execute(
        "edit_habit",
        action="add",
        name="Drink water",
        cycle="weekly",
        target=4,
        category="health",
    )
    assert revived.success
    assert f"Habit #{habit_id} reactivated" in revived.output

    conn = _connect()
    habit = conn.execute(
        "SELECT id, active, frequency, category, context "
        "FROM habits WHERE user_id = 1 AND name = 'Drink water'"
    ).fetchone()
    history_count = conn.execute(
        "SELECT COUNT(*) FROM habit_logs WHERE habit_id = ?",
        (habit_id,),
    ).fetchone()[0]
    conn.close()
    assert habit["id"] == habit_id
    assert habit["active"] == 1
    assert habit["frequency"] == "weekly:4"
    assert habit["category"] == "health"
    assert habit["context"] == ""
    assert history_count == 3


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


def test_memory_and_trash_lists_expose_continuation_metadata():
    memory_ids = [
        save_memory_item(
            1,
            f"Fact {hashlib.sha256(str(index).encode()).hexdigest()}",
        )
        for index in range(35)
    ]
    assert len(list_all_memories(1, limit=10, offset=10)) == 10

    skill = MemorySkill()
    memories = asyncio.run(skill.execute(SkillContext(
        trigger="tool_call",
        user_id=1,
        tool_name="list_memories",
        args={"limit": 10, "offset": 10},
    )))
    assert memories.output.startswith(
        "Memories: total=35, shown=10, offset=10, next_offset=20"
    )

    assert delete_memory_items(memory_ids[:25], deleted_by="user") == 25
    trash = asyncio.run(skill.execute(SkillContext(
        trigger="tool_call",
        user_id=1,
        tool_name="memory_trash_bin",
        args={"action": "list"},
    )))
    assert trash.output.startswith(
        "Trash: total=25, shown=20, offset=0, next_offset=20"
    )


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


@pytest.mark.asyncio
async def test_todo_exact_match_reopen_and_clear_nudge_date():
    from mochi.skills.base import SkillContext
    from mochi.skills.todo.handler import TodoSkill
    from mochi.skills.todo.queries import get_todos

    skill = TodoSkill()

    async def execute(args):
        return await skill.execute(SkillContext(
            trigger="tool_call",
            user_id=1,
            tool_name="manage_todo",
            args=args,
        ))

    added = await execute({
        "action": "add",
        "task": "Buy\u3000Milk",
        "nudge_date": "2026-08-20",
    })
    completed = await execute({
        "action": "complete",
        "match": "  buy milk ",
    })
    reopened = await execute({
        "action": "reopen",
        "todo_id": 1,
    })
    updated = await execute({
        "action": "update",
        "match": "BUY MILK",
        "clear_nudge_date": True,
    })

    assert added.success
    assert completed.success
    assert reopened.success
    assert updated.success
    assert get_todos(1, include_done=True)[0]["nudge_date"] is None

    completed_again = await execute({
        "action": "complete",
        "todo_id": 1,
    })
    assert completed_again.success
    already_completed = await execute({
        "action": "complete",
        "todo_id": 1,
    })
    assert not already_completed.success

    await execute({"action": "reopen", "todo_id": 1})
    await execute({"action": "add", "task": "Buy Milk"})
    ambiguous = await execute({
        "action": "complete",
        "match": "buy milk",
    })
    destructive_match = await execute({
        "action": "delete",
        "match": "buy milk",
    })

    assert not ambiguous.success
    assert "Multiple exact matches" in ambiguous.output
    assert "#1" in ambiguous.output and "#2" in ambiguous.output
    assert not destructive_match.success
    assert "todo_id" in destructive_match.output

    unchanged = await execute({
        "action": "update",
        "todo_id": 1,
        "clear_nudge_date": True,
    })
    assert unchanged.success
    assert not unchanged.state_changed
    assert "unchanged" in unchanged.output


@pytest.mark.asyncio
async def test_meal_source_is_hidden_and_framework_bound():
    import mochi.skills as skill_registry
    from mochi.skills.base import SkillContext
    from mochi.skills.meal.handler import MealSkill
    from mochi.skills.meal.queries import query_health_log

    tool = next(
        tool
        for tool in skill_registry.get_tools()
        if tool["function"]["name"] == "log_meal"
    )
    properties = tool["function"]["parameters"]["properties"]
    assert "source" not in properties
    assert properties["meal_type"]["enum"] == [
        "breakfast", "lunch", "dinner", "snack",
    ]

    result = await MealSkill().execute(SkillContext(
        trigger="tool_call",
        user_id=1,
        tool_name="log_meal",
        args={
            "meal_type": "lunch",
            "items": [{
                "name": "sandwich",
                "calories": 420,
                "protein_g": 18,
                "carbs_g": 45,
                "fat_g": 16,
            }],
            "_source": "photo",
            "source": "voice",
            "date": "2026-08-17",
        },
    ))

    records = query_health_log(
        user_id=1,
        types=["meal"],
        date="2026-08-17",
    )
    assert result.success
    assert json.loads(records[0]["metrics"])["source"] == "photo"


def test_tool_outcome_trusts_todo_state_fact_over_user_text():
    from mochi.skills.base import SkillResult
    from mochi.tool_execution import outcome_for

    outcome = outcome_for(
        "todo",
        "manage_todo",
        {
            "action": "update",
            "task": "Keep API unchanged.",
        },
        SkillResult(
            output="Todo #1 updated: task=Keep API unchanged.",
            state_changed=True,
        ),
    )

    assert outcome["state_changed"]
