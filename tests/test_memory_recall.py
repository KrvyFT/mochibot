"""Text-authoritative recall and derived-index consistency."""

from __future__ import annotations

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
    update_memory_item,
)


class Pool:
    def __init__(self, embedding=None, error=None):
        self.embedding = embedding
        self.error = error

    def embed(self, _text):
        if self.error:
            raise self.error
        return self.embedding


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
    monkeypatch.setattr(config, "KG_ENABLED", False)


@pytest.mark.parametrize(
    "pool",
    [Pool(), Pool(error=RuntimeError("provider offline"))],
)
def test_auto_recall_uses_text_when_embedding_is_unavailable(
    monkeypatch, pool,
):
    import mochi.model_pool as model_pool

    save_memory_item(
        1, "偏好", "喜欢茉莉花茶", source="admin",
    )
    monkeypatch.setattr(model_pool, "get_pool", lambda: pool)

    recalled = _retrieve_memories_for_turn("我喜欢什么花茶？", 1)

    assert [item["text"] for item in recalled] == ["喜欢茉莉花茶"]


def test_like_fallback_is_authoritative_without_fts_or_embedding(monkeypatch):
    import mochi.db as db
    import mochi.model_pool as model_pool

    save_memory_item(
        1, "偏好", "Likes jasmine tea", source="admin",
    )
    monkeypatch.setattr(db, "_FTS_AVAILABLE", False)
    monkeypatch.setattr(model_pool, "get_pool", lambda: Pool())

    recalled = _retrieve_memories_for_turn(
        "Do I like jasmine tea?", 1,
    )

    assert [item["text"] for item in recalled] == [
        "Likes jasmine tea",
    ]


def test_semantic_query_never_uses_recent_only_filler(monkeypatch):
    import mochi.model_pool as model_pool

    save_memory_item(
        1, "事实", "Unrelated but very recent detail", source="admin",
    )
    monkeypatch.setattr(model_pool, "get_pool", lambda: Pool())

    assert _retrieve_memories_for_turn("jasmine", 1) == []
    assert recall_memory(
        1, query="jasmine", bump_access=False,
    ) == []


def test_edit_delete_merge_restore_keep_fts_vector_and_kg_consistent(
    monkeypatch,
):
    import mochi.db as db

    embedding = struct.pack("1536f", 1.0, *([0.0] * 1535))
    other_embedding = struct.pack(
        "1536f", 0.0, 1.0, *([0.0] * 1534),
    )
    kept_id = save_memory_item(
        1, "事实", "Old alpha memory", source="admin",
        embedding=embedding,
    )
    deleted_id = save_memory_item(
        1, "事实", "Temporary beta memory", source="admin",
        embedding=other_embedding,
    )
    conn = _connect()
    subject = conn.execute(
        "INSERT INTO kg_entities "
        "(user_id, name, display_name, entity_type, created_at) "
        "VALUES (1, 'alpha', 'Alpha', 'concept', 'now')"
    ).lastrowid
    object_id = conn.execute(
        "INSERT INTO kg_entities "
        "(user_id, name, display_name, entity_type, created_at) "
        "VALUES (1, 'beta', 'Beta', 'concept', 'now')"
    ).lastrowid
    conn.execute(
        "INSERT INTO kg_triples "
        "(user_id, subject_id, predicate, object_id, source_memory_id, "
        "source, confidence, created_at) "
        "VALUES (1, ?, 'related_to', ?, ?, 'memory_item', 1.0, 'now')",
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
        category="事实",
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
