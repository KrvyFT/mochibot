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
