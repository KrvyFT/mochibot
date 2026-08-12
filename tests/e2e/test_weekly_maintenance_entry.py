from datetime import datetime, timezone

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
        "VALUES (1, 'user', 'started learning Japanese', "
        "'2026-08-05T10:00:00+00:00', 1)"
    )
    evidence_id = cursor.lastrowid
    conn.commit()
    conn.close()
    insert_memory_item(
        1,
        "goal",
        "started learning Japanese",
        2,
        source="extracted",
        evidence_message_ids=[evidence_id],
    )
    old_core = "# Us\n- Natural companionship"
    replace_core(old_core)
    mock_llm_factory([
        make_response(tool_calls=[
            make_tool_call(
                "update_weekly_core",
                {
                    "expected_content": old_core,
                    "operations": [{
                        "action": "insert_after",
                        "anchor_text": "- Natural companionship",
                        "content": "- User started learning Japanese",
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
