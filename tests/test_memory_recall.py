"""Text-authoritative recall and derived-index consistency."""

from __future__ import annotations

import json
import struct

import pytest

from mochi.ai_client import _retrieve_memories_for_turn, _user_last_recall
from mochi.db import (
    _connect,
    delete_memory_items,
    list_memory_trash,
    merge_memory_items,
    recall_memory,
    restore_memory_from_trash,
    save_memory_item,
    save_message,
    update_memory_item,
)
from mochi.knowledge_graph import (
    curate_relationships,
    find_matching_entities,
    list_active_relationships,
)


class Pool:
    def __init__(self, embedding=None, error=None):
        self.embedding = embedding
        self.error = error

    def embed(self, _text):
        if self.error:
            raise self.error
        return self.embedding


class Selector:
    def __init__(self, memory_ids):
        self.memory_ids = memory_ids
        self.calls = []

    def chat(self, **kwargs):
        from mochi.llm import LLMResponse

        self.calls.append(kwargs)
        return LLMResponse(
            content=json.dumps({"memory_ids": self.memory_ids}),
            prompt_tokens=20,
            completion_tokens=5,
            total_tokens=25,
            model="lite-selector",
        )


def _recalled_item(item_id, content, score):
    return {
        "id": item_id,
        "content": content,
        "score": score,
        "vec_sim": 0.0,
        "match_source": "fts",
        "fts_hit": True,
        "has_vector": False,
        "evidence_start": "2026-08-01",
        "evidence_end": "2026-08-01",
    }


@pytest.fixture(autouse=True)
def _recall_config(monkeypatch):
    import mochi.config as config

    _user_last_recall.clear()
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL", True)
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL_TOP_K", 5)
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL_MAX_ITEMS", 3)
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL_MIN_VEC_SIM", 0.35)
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL_MAX_CHARS", 320)
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL_MAX_TOKENS", 600)
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL_COOLDOWN", 0)
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL_SELECTOR_MAX_TOKENS", 160)
    monkeypatch.setattr(config, "KG_ENABLED", False)


def test_edit_delete_merge_restore_keep_fts_vector_and_kg_consistent(
    monkeypatch,
):
    import mochi.db as db

    embedding = struct.pack("1536f", 1.0, *([0.0] * 1535))
    other_embedding = struct.pack(
        "1536f", 0.0, 1.0, *([0.0] * 1534),
    )
    kept_id = save_memory_item(
        1, "Old alpha memory", source="admin",
        embedding=embedding,
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

    trash = list_memory_trash(1)
    beta_trash = next(
        item for item in trash if "beta" in item["content"]
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


def test_auto_recall_fuses_current_and_continuity_lanes_for_user_zero(
    monkeypatch,
):
    import mochi.ai_client as ai_client
    import mochi.model_pool as model_pool

    save_message(
        0,
        "user",
        "我爸昨晚很晚还没回家。",
        turn_id="history",
    )
    save_message(
        0,
        "assistant",
        "等有消息告诉我。",
        turn_id="history",
    )
    current_id = save_message(
        0,
        "user",
        "说个新话题，网球拍怎么选？",
        turn_id="current",
    )
    calls = []

    def fake_recall(_user_id, query, **_kwargs):
        calls.append(query)
        if query == "说个新话题，网球拍怎么选？":
            return [
                _recalled_item(1, "用户偏好轻量网球拍", 8.0),
                _recalled_item(2, "用户周末常打网球", 7.0),
            ]
        return [
            _recalled_item(1, "用户偏好轻量网球拍", 6.0),
            _recalled_item(3, "用户父亲昨晚未回家", 9.0),
        ]

    monkeypatch.setattr(ai_client, "recall_memory", fake_recall)
    monkeypatch.setattr(model_pool, "get_pool", lambda: Pool())
    monkeypatch.setattr(
        ai_client,
        "get_client_for_tier",
        lambda _tier="main": (_ for _ in ()).throw(RuntimeError("no lite")),
    )

    recalled = _retrieve_memories_for_turn(
        "说个新话题，网球拍怎么选？",
        0,
        current_id,
    )

    assert len(calls) == 2
    assert calls[0] == "说个新话题，网球拍怎么选？"
    assert "我爸昨晚很晚还没回家" in calls[1]
    assert "等有消息告诉我" in calls[1]
    assert calls[1].endswith("[当前用户] 说个新话题，网球拍怎么选？")
    assert [item["memory_id"] for item in recalled] == [1, 2]
    assert recalled[0]["retrieval_lanes"] == ["current", "continuity"]
    assert recalled[0]["lane_ranks"] == {"current": 1, "continuity": 1}


def test_auto_recall_cooldown_only_suppresses_identical_query(
    monkeypatch,
):
    import mochi.ai_client as ai_client
    import mochi.config as config
    import mochi.model_pool as model_pool

    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL_COOLDOWN", 120)
    calls = []

    def fake_recall(_user_id, query, **_kwargs):
        calls.append(query)
        return [_recalled_item(1, f"memory for {query}", 8.0)]

    monkeypatch.setattr(ai_client, "recall_memory", fake_recall)
    monkeypatch.setattr(model_pool, "get_pool", lambda: Pool())
    monkeypatch.setattr(
        ai_client,
        "get_client_for_tier",
        lambda _tier="main": (_ for _ in ()).throw(RuntimeError("no lite")),
    )

    assert _retrieve_memories_for_turn("alpha", 0)
    assert _retrieve_memories_for_turn("alpha", 0) == []
    assert _retrieve_memories_for_turn("beta", 0)
    assert calls == ["alpha", "beta"]


def test_lite_selector_can_abstain_or_choose_continuity_memory(
    monkeypatch,
):
    import mochi.ai_client as ai_client
    import mochi.model_pool as model_pool

    save_message(
        1,
        "user",
        "我爸昨晚很晚还没回家。",
        turn_id="history",
    )
    save_message(
        1,
        "assistant",
        "等有消息告诉我。",
        turn_id="history",
    )
    current_id = save_message(
        1,
        "user",
        "那他后来呢？",
        turn_id="current",
    )

    def fake_recall(_user_id, query, **_kwargs):
        if query == "那他后来呢？":
            return [_recalled_item(1, "用户常打网球", 4.0)]
        return [
            _recalled_item(1, "用户常打网球", 4.0),
            _recalled_item(3, "用户父亲昨晚未回家", 8.0),
        ]

    selector = Selector([3])
    monkeypatch.setattr(ai_client, "recall_memory", fake_recall)
    monkeypatch.setattr(model_pool, "get_pool", lambda: Pool())
    monkeypatch.setattr(
        ai_client,
        "get_client_for_tier",
        lambda _tier="main": selector,
    )

    recalled = _retrieve_memories_for_turn("那他后来呢？", 1, current_id)

    assert [item["memory_id"] for item in recalled] == [3]
    selector_payload = json.loads(
        selector.calls[0]["messages"][1]["content"]
    )
    assert selector_payload["current_message"] == "那他后来呢？"
    assert "我爸昨晚很晚还没回家" in selector_payload["recent_context"]
    assert {
        item["memory_id"] for item in selector_payload["candidates"]
    } == {1, 3}

    selector.memory_ids = []
    _user_last_recall.clear()
    assert _retrieve_memories_for_turn("那他后来呢？", 1, current_id) == []


def test_invalid_selector_ids_use_topic_safe_fallback(monkeypatch):
    import mochi.ai_client as ai_client
    import mochi.model_pool as model_pool

    selector = Selector([999])
    monkeypatch.setattr(
        ai_client,
        "recall_memory",
        lambda *_args, **_kwargs: [
            _recalled_item(1, "current topic memory", 8.0),
        ],
    )
    monkeypatch.setattr(model_pool, "get_pool", lambda: Pool())
    monkeypatch.setattr(
        ai_client,
        "get_client_for_tier",
        lambda _tier="main": selector,
    )

    recalled = _retrieve_memories_for_turn("current topic", 1)

    assert [item["memory_id"] for item in recalled] == [1]


def test_continuity_query_keeps_legacy_paired_assistant():
    from mochi.ai_client import _memory_recall_queries

    save_message(1, "user", "legacy user context")
    save_message(1, "assistant", "legacy assistant context")
    current_id = save_message(
        1,
        "user",
        "what happened next?",
        turn_id="current",
    )

    queries = _memory_recall_queries(
        "what happened next?",
        1,
        current_id,
    )

    assert len(queries) == 2
    assert "legacy user context" in queries[1][1]
    assert "legacy assistant context" in queries[1][1]


def test_continuity_query_excludes_processed_events_before_turn_limit():
    from mochi.ai_client import _memory_recall_queries

    for number in range(3):
        turn_id = f"ordinary-{number}"
        save_message(1, "user", f"ordinary user {number}", turn_id=turn_id)
        save_message(
            1,
            "assistant",
            f"ordinary assistant {number}",
            turn_id=turn_id,
        )
    for number in range(5):
        save_message(
            1,
            "assistant",
            f"processed runtime event {number}",
            turn_id=f"runtime-{number}",
            processed=True,
        )
    current_id = save_message(
        1,
        "user",
        "what happened next?",
        turn_id="current",
    )

    queries = _memory_recall_queries(
        "what happened next?",
        1,
        current_id,
    )

    assert "ordinary user 0" in queries[1][1]
    assert "ordinary assistant 2" in queries[1][1]
    assert "processed runtime event" not in queries[1][1]


def test_selector_abstention_suppresses_kg_injection(monkeypatch):
    import mochi.ai_client as ai_client
    import mochi.config as config
    import mochi.knowledge_graph as kg
    import mochi.model_pool as model_pool

    selector = Selector([])
    monkeypatch.setattr(config, "KG_ENABLED", True)
    monkeypatch.setattr(
        ai_client,
        "recall_memory",
        lambda *_args, **_kwargs: [
            _recalled_item(1, "candidate memory", 8.0),
        ],
    )
    monkeypatch.setattr(model_pool, "get_pool", lambda: Pool())
    monkeypatch.setattr(
        ai_client,
        "get_client_for_tier",
        lambda _tier="main": selector,
    )
    monkeypatch.setattr(
        kg,
        "find_matching_entities",
        lambda *_args, **_kwargs: ["matched"],
    )
    monkeypatch.setattr(
        kg,
        "entity_context_for_prompt",
        lambda *_args, **_kwargs: "KG context that must not leak",
    )

    assert _retrieve_memories_for_turn("unrelated question", 1) == []


def test_query_ranking_ignores_importance_access_and_recency():
    first = save_memory_item(
        1,
        "alpha shared detail",
        source="extracted",
        importance=1,
    )
    second = save_memory_item(
        1,
        "alpha shared detail",
        source="extracted",
        importance=3,
    )
    conn = _connect()
    conn.execute(
        "UPDATE memory_items SET access_count = 100, "
        "updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (second,),
    )
    conn.commit()
    conn.close()

    recalled = recall_memory(1, query="alpha", bump_access=False)
    scores = {
        item["id"]: item["score"]
        for item in recalled
        if item["id"] in {first, second}
    }

    assert set(scores) == {first, second}
    assert scores[first] == scores[second]


def _relationship_memory(content="Shiki lives with Mochi in Shanghai"):
    evidence_id = save_message(1, "user", content)
    item_id = save_memory_item(
        1,
        content,
        source="extracted",
        evidence_message_ids=[evidence_id],
    )
    conn = _connect()
    row = conn.execute(
        "SELECT content, updated_at FROM memory_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    conn.close()
    return item_id, {
        "item_id": item_id,
        "content": row["content"],
        "updated_at": row["updated_at"],
    }


def _relationship_upsert(item_id):
    return {
        "op": "upsert",
        "subject": {"name": "Shiki", "type": "person"},
        "predicate": "lives_with",
        "object": {"name": "Mochi", "type": "pet"},
        "source_memory": {"item_id": item_id},
    }


def test_weekly_relationship_upsert_archive_and_entity_recall():
    item_id, snapshot = _relationship_memory()
    conn = _connect()
    conn.execute(
        "INSERT INTO kg_entities "
        "(user_id, name, display_name, entity_type, created_at) "
        "VALUES (1, 'unused', 'Unused', 'place', 'now')"
    )
    conn.commit()
    conn.close()

    lives_in = {
        "op": "upsert",
        "subject": {"name": "Shiki", "type": "person"},
        "predicate": "lives_in",
        "object": {"name": "Shanghai", "type": "place"},
        "source_memory": {"item_id": item_id},
    }
    created = curate_relationships(
        1,
        {item_id: snapshot},
        {},
        [_relationship_upsert(item_id), lives_in],
    )

    assert len(created.upserted_ids) == 2
    assert set(find_matching_entities(
        1, "Shiki and Mochi are going home to Shanghai",
    )) == {"shiki", "mochi", "shanghai"}
    assert find_matching_entities(1, "The unused place") == []
    relationships = list_active_relationships(1)
    relationship_snapshots = {
        relationship["triple_id"]: relationship
        for relationship in relationships
    }
    repeated = curate_relationships(
        1,
        {item_id: snapshot},
        relationship_snapshots,
        [_relationship_upsert(item_id), lives_in],
    )
    assert repeated.upserted_ids == ()

    newer_id, newer_snapshot = _relationship_memory(
        "Shiki confirmed that Mochi still lives in the same household",
    )
    refreshed = curate_relationships(
        1,
        {newer_id: newer_snapshot},
        relationship_snapshots,
        [_relationship_upsert(newer_id)],
    )
    assert len(refreshed.upserted_ids) == 1
    conn = _connect()
    assert conn.execute(
        "SELECT source_memory_id FROM kg_triples WHERE id = ?",
        (refreshed.upserted_ids[0],),
    ).fetchone()["source_memory_id"] == newer_id
    conn.close()
    relationships = list_active_relationships(1)

    archived = curate_relationships(
        1,
        {item_id: snapshot},
        {
            relationship["triple_id"]: relationship
            for relationship in relationships
        },
        [
            {"op": "archive", "triple_id": relationship["triple_id"]}
            for relationship in relationships
        ],
    )

    assert archived.archived_ids == tuple(
        relationship["triple_id"] for relationship in relationships
    )
    assert list_active_relationships(1) == []
    assert find_matching_entities(
        1, "Shiki, Mochi, and Shanghai",
    ) == []
