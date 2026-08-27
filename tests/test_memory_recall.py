"""Memory authority and derived-index consistency contract."""

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
