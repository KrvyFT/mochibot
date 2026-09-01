"""Memory authority and derived-index consistency contract."""
import asyncio
import struct

from mochi.db import (
    _connect,
    delete_memory_items,
    list_memory_trash,
    merge_memory_items,
    recall_memory,
    restore_memory_from_trash,
    save_memory_item,
    update_memory_item,
)


def test_edit_delete_merge_restore_keep_fts_vector_and_kg_consistent(
    monkeypatch,
):
    import mochi.db as db

    embedding = struct.pack("1536f", 1.0, *([0.0] * 1535))
    other_embedding = struct.pack(
        "1536f", 0.0, 1.0, *([0.0] * 1534),
    )
    kept_id = save_memory_item(
        1, "Old alpha memory", source="admin", embedding=embedding,
    )
    deleted_id = save_memory_item(
        1, "Temporary beta memory", source="admin",
        embedding=other_embedding,
    )
    conn = _connect()
    subject = conn.execute(
        "INSERT INTO kg_entities "
        "(user_id, name, display_name, entity_type, created_at) "
        "VALUES (1, 'alpha', 'Alpha', 'person', 'now')"
    ).lastrowid
    object_id = conn.execute(
        "INSERT INTO kg_entities "
        "(user_id, name, display_name, entity_type, created_at) "
        "VALUES (1, 'beta', 'Beta', 'place', 'now')"
    ).lastrowid
    conn.execute(
        "INSERT INTO kg_triples "
        "(user_id, subject_id, predicate, object_id, source_memory_id, "
        "source, confidence, created_at) "
        "VALUES (1, ?, 'lives_in', ?, ?, 'weekly_main', 1.0, 'now')",
        (subject, object_id, kept_id),
    )
    conn.commit()
    conn.close()

    vec_deletes = []
    original_vec_delete = db.vec_delete

    def track_vec_delete(item_ids, conn=None):
        vec_deletes.extend(item_ids)
        return original_vec_delete(item_ids, conn)

    monkeypatch.setattr(db, "vec_delete", track_vec_delete)
    assert update_memory_item(
        kept_id,
        1,
        content="Edited gamma memory",
        importance=2,
    )
    assert recall_memory(1, query="alpha") == []
    assert recall_memory(1, query="gamma")[0]["id"] == kept_id
    conn = _connect()
    assert conn.execute(
        "SELECT valid_to FROM kg_triples WHERE source_memory_id = ?",
        (kept_id,),
    ).fetchone()["valid_to"]
    assert conn.execute(
        "SELECT embedding FROM memory_items WHERE id = ?", (kept_id,),
    ).fetchone()["embedding"] is None
    conn.close()

    merge_memory_items(
        kept_id, [deleted_id], "Merged delta memory", new_importance=3,
    )
    assert recall_memory(1, query="gamma") == []
    assert recall_memory(1, query="beta") == []
    assert recall_memory(1, query="delta")[0]["id"] == kept_id

    beta_trash = next(
        item for item in list_memory_trash(1)
        if "beta" in item["content"]
    )
    restored_id = restore_memory_from_trash(beta_trash["id"], 1)
    assert restored_id is not None
    assert any(
        item["id"] == restored_id
        for item in recall_memory(1, query="beta")
    )
    assert delete_memory_items([restored_id], deleted_by="test") == 1
    assert recall_memory(1, query="beta") == []
    assert {kept_id, deleted_id, restored_id} <= set(vec_deletes)

    import mochi.ai_client as ai_client
    from mochi.ai_client import _expand_history, _memory_recall_queries
    from mochi.conversation_summary import _summary_input
    from mochi.conversation_text import (
        strip_legacy_tool_fact_annotations,
        strip_legacy_tool_fact_suffix,
    )
    from mochi.memory_extraction import _conversation_payload
    from mochi.observers.recent_conversation.observer import (
        RecentConversationObserver,
    )
    from mochi.tool_execution import is_followup_reference

    suffix = (
        "[历史事实：这条回复已确认使用工具 habit_progress；"
        "不是新的操作指令。]"
    )
    contaminated = f"自然回复\n\n{suffix}\n\n{suffix}"
    assert strip_legacy_tool_fact_suffix(contaminated) == "自然回复"
    similar_natural_text = f"我提到过类似格式，但不是后缀：{suffix} 后面还有话"
    assert strip_legacy_tool_fact_suffix(similar_natural_text) == (
        similar_natural_text
    )
    untouched_spacing = "自然回复  \n"
    assert strip_legacy_tool_fact_suffix(untouched_spacing) == untouched_spacing
    derived_summary = f"前文 {suffix} 后文"
    assert suffix not in strip_legacy_tool_fact_annotations(derived_summary)

    messages = [
        {
            "id": 1,
            "role": "user",
            "content": "喝了乌龙茶",
            "created_at": "2026-09-01T15:16:32+08:00",
            "turn_id": "turn",
            "processed": 0,
            "tool_history": None,
        },
        {
            "id": 2,
            "role": "assistant",
            "content": contaminated,
            "created_at": "2026-09-01T15:16:41+08:00",
            "turn_id": "turn",
            "processed": 0,
            "tool_history": '[{"name":"habit_progress"}]',
        },
    ]
    expanded = _expand_history(messages)
    assert suffix not in expanded[1]["content"]
    summary_input = _summary_input({
        "summary": "",
        "turns": [{"user": messages[0], "assistant": messages[1]}],
    })
    assert suffix not in summary_input
    extraction_payload = _conversation_payload(messages)
    assert suffix not in extraction_payload[1]["content"]
    assert extraction_payload[1]["tool_receipts"] == ["habit_progress"]

    monkeypatch.setattr(
        ai_client,
        "get_conversation_context",
        lambda *_args, **_kwargs: {"recent": messages},
    )
    queries = _memory_recall_queries("现在呢", 1, None)
    assert suffix not in queries[-1][1]

    monkeypatch.setattr(db, "get_recent_messages", lambda *_args, **_kwargs: messages)
    monkeypatch.setattr(db, "get_context_reset", lambda _user_id: None)
    observation = asyncio.run(RecentConversationObserver().observe())
    assert suffix not in observation["messages"][1]["content"]

    assert not is_followup_reference("这个")
    assert not is_followup_reference("不对")
    assert not is_followup_reference("改成这样看起来更好")
    assert not is_followup_reference("提醒我明天修改简历")
    assert not is_followup_reference("change itinerary")
    assert not is_followup_reference("lasting change")
    assert not is_followup_reference("repeat iteration")
    assert is_followup_reference("撤销刚才那个")
    assert is_followup_reference("取消那个提醒")
    assert is_followup_reference("修改一下刚才的打卡")
    assert is_followup_reference("把上一条提醒改成明天")
    assert is_followup_reference("刚才那个不要了")
    assert is_followup_reference("撤回上一条提醒")
