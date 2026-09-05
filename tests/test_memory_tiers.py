"""Memory three-tier: tags, temp memories, extraction contract."""

import pytest

from mochi.memory_contract import (
    format_memory_tags_zh,
    infer_memory_tags,
    validate_memory_kind,
    validate_memory_tags,
)
from mochi.memory_extraction import validate_extraction_response
from mochi.db import (
    clear_temp_memories_before,
    insert_memory_item,
    insert_temp_memory_item,
    list_all_memories,
    list_temp_memories,
)


def test_validate_memory_tags_accepts_zh_and_en():
    assert validate_memory_tags(["偏好", "fact"]) == ("preference", "fact")
    assert format_memory_tags_zh(("preference", "habit")) == "偏好、习惯"


def test_infer_memory_tags_defaults_to_fact():
    assert "preference" in infer_memory_tags("喜欢喝美式")
    assert infer_memory_tags("普通一句") == ("fact",)


def test_extraction_response_requires_kind_and_tags():
    batch = [
        {"id": 1, "role": "user", "content": "喜欢简洁沟通"},
        {"id": 2, "role": "assistant", "content": "好"},
    ]
    raw = """[
      {
        "kind": "core",
        "content": "喜欢简洁直接的沟通方式",
        "importance": 2,
        "tags": ["偏好"],
        "evidence_message_ids": [1]
      },
      {
        "kind": "temp",
        "content": "今晚要交设计作业",
        "importance": 1,
        "tags": ["事件"],
        "evidence_message_ids": [1]
      }
    ]"""
    items = validate_extraction_response(raw, batch)
    assert [i["kind"] for i in items] == ["core", "temp"]
    assert items[0]["tags"] == ["preference"]
    assert validate_memory_kind("temp") == "temp"


@pytest.mark.usefixtures("fresh_db")
def test_temp_memory_day_scope_and_clear():
    insert_temp_memory_item(
        1, "今晚交作业", 1, tags=["事件"], day="2026-09-04",
    )
    insert_temp_memory_item(
        1, "今天路过花店", 1, tags=["事件"], day="2026-09-05",
    )
    assert len(list_temp_memories(1, day="2026-09-05")) == 1
    purged = clear_temp_memories_before("2026-09-05", 1)
    assert purged == 1
    assert list_temp_memories(1, day="2026-09-04") == []
    assert len(list_temp_memories(1, day="2026-09-05")) == 1


@pytest.mark.usefixtures("fresh_db")
def test_core_memory_tags_roundtrip():
    item_id = insert_memory_item(
        1, "喜欢简洁直接沟通", 2, tags=["偏好", "习惯"],
    )
    assert item_id > 0
    items = list_all_memories(1, tag="偏好")
    assert len(items) == 1
    assert "preference" in items[0]["tags"]
    assert list_all_memories(1, tag="情感") == []
