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
    item = conn.execute(
        "SELECT content, updated_at FROM memory_items WHERE id = ?",
        (item_id,),
    ).fetchone()
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
                {"operations": []},
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
                            "content": item["content"],
                            "updated_at": item["updated_at"],
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
    assert [row["role"] for row in get_recent_messages(1)] == ["user"]
    conn = _connect()
    relation = conn.execute(
        "SELECT predicate, source, source_memory_id FROM kg_triples "
        "WHERE valid_to IS NULL"
    ).fetchone()
    conn.close()
    assert dict(relation) == {
        "predicate": "lives_in",
        "source": "weekly_main",
        "source_memory_id": item_id,
    }
