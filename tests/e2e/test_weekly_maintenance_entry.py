import pytest

from mochi.ai_client import chat
from mochi.core_store import read_core, replace_core
from mochi.db import _connect, get_recent_messages, insert_memory_item
from mochi.main_runtime import MainRuntimeEntry
from tests.e2e.mock_llm import make_response, make_tool_call


@pytest.mark.asyncio
async def test_weekly_main_updates_core_without_chat_history(
    mock_llm_factory,
):
    conn = _connect()
    cursor = conn.execute(
        "INSERT INTO messages "
        "(user_id, role, content, created_at, processed) "
        "VALUES (1, 'user', 'Shiki moved to Tokyo and started learning Japanese', "
        "'2026-08-05T10:00:00+00:00', 1)"
    )
    evidence_id = cursor.lastrowid
    refreshed_evidence_id = conn.execute(
        "INSERT INTO messages "
        "(user_id, role, content, created_at, processed) "
        "VALUES (1, 'user', 'Tokyo life is settling in and Japanese is going well', "
        "'2026-08-06T10:00:00+00:00', 1)"
    ).lastrowid
    conn.commit()
    conn.close()
    item_id = insert_memory_item(
        1,
        "Shiki moved to Tokyo and started learning Japanese",
        2,
        source="extracted",
        evidence_message_ids=[evidence_id],
    )
    conn = _connect()
    conn.execute(
        "UPDATE memory_items SET created_at = ?, updated_at = ? WHERE id = ?",
        (
            "2026-08-05T10:05:00+00:00",
            "2026-08-05T10:05:00+00:00",
            item_id,
        ),
    )
    conn.commit()
    conn.close()
    old_core = "# Us\n- Natural companionship"
    replace_core(old_core)
    mock_llm_factory([
        make_response(tool_calls=[
            make_tool_call(
                "update_weekly_core",
                {
                    "content": (
                        f"{old_core}\n"
                        "- User started learning Japanese"
                    ),
                },
            ),
        ]),
        make_response(tool_calls=[
            make_tool_call(
                "curate_weekly_memory",
                {
                    "operations": [{
                        "op": "edit",
                        "item_id": item_id,
                        "content": "Shiki lives in Tokyo and is learning Japanese",
                        "importance": 3,
                        "evidence_message_ids": [refreshed_evidence_id],
                    }],
                },
            ),
        ]),
        make_response(tool_calls=[
            make_tool_call(
                "curate_relationships",
                {
                    "operations": [{
                        "op": "upsert",
                        "subject": {"name": "Shiki", "type": "person"},
                        "predicate": "lives_in",
                        "object": {"name": "Tokyo", "type": "place"},
                        "source_memory": {
                            "item_id": item_id,
                        },
                    }],
                },
            ),
        ]),
        make_response("done"),
    ])

    result = await chat(runtime_entry=MainRuntimeEntry.weekly_maintenance(
        logical_date="2026-08-10",
        period_key="2026-W33",
        user_id=1,
        channel_id=100,
        transport="fake",
    ))

    assert result.text == ""
    assert "User started learning Japanese" in read_core()
    assert [row["role"] for row in get_recent_messages(1)] == ["user", "user"]
    conn = _connect()
    memory = conn.execute(
        "SELECT content, importance FROM memory_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    relation = conn.execute(
        "SELECT predicate, source, source_memory_id FROM kg_triples "
        "WHERE valid_to IS NULL"
    ).fetchone()
    conn.close()
    assert dict(memory) == {
        "content": "Shiki lives in Tokyo and is learning Japanese",
        "importance": 3,
    }
    assert dict(relation) == {
        "predicate": "lives_in",
        "source": "weekly_main",
        "source_memory_id": item_id,
    }
