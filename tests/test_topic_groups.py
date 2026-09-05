"""Topic grouping and semantic reply-anchor choice."""

import pytest

from mochi.topic_groups import (
    TopicGroup,
    _PARSE_FAIL,
    _parse_group_indices,
    _parse_reply_to,
    choose_reply_anchor,
    split_user_topics,
)


def test_parse_group_indices_requires_full_cover():
    assert _parse_group_indices('{"groups":[[0,1],[2]]}', 3) == [[0, 1], [2]]
    assert _parse_group_indices('{"groups":[[0],[0]]}', 2) is None
    assert _parse_group_indices('{"groups":[[0]]}', 2) is None


def test_parse_reply_to_accepts_null_and_index():
    assert _parse_reply_to('{"reply_to": null}', 2) is None
    assert _parse_reply_to('{"reply_to": 1}', 2) == 1
    assert _parse_reply_to('{"reply_to": 9}', 2) is _PARSE_FAIL
    assert _parse_reply_to("not json", 1) is _PARSE_FAIL


@pytest.mark.asyncio
async def test_single_item_is_one_group():
    groups = await split_user_topics([("想你", 10)])
    assert len(groups) == 1
    assert groups[0].combined_text == "想你"
    assert groups[0].anchor_msg_id == 10


@pytest.mark.asyncio
async def test_split_falls_back_per_message_when_llm_missing(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("no lite")

    monkeypatch.setattr("mochi.llm.get_client_for_tier", boom)
    groups = await split_user_topics([("药吃了吗", 1), ("晚饭呢", 2)])
    assert len(groups) == 2
    assert groups[0].anchor_msg_id == 1
    assert groups[1].anchor_msg_id == 2


def test_topic_group_anchor_is_last_msg():
    group = TopicGroup(texts=("a", "b"), user_msg_ids=(3, 7))
    assert group.anchor_msg_id == 7
    assert group.combined_text == "a\n\nb"


@pytest.mark.asyncio
async def test_choose_reply_anchor_can_pick_or_skip(monkeypatch):
    group = TopicGroup(texts=("药吃了吗", "晚饭呢"), user_msg_ids=(11, 22))

    class FakeClient:
        def __init__(self, content):
            self._content = content

        def chat(self, **_kwargs):
            return type("R", (), {"content": self._content})()

    monkeypatch.setattr(
        "mochi.llm.get_client_for_tier",
        lambda *_a, **_k: FakeClient('{"reply_to": 0}'),
    )
    assert await choose_reply_anchor(group, "药记得吃。") == 11

    monkeypatch.setattr(
        "mochi.llm.get_client_for_tier",
        lambda *_a, **_k: FakeClient('{"reply_to": null}'),
    )
    assert await choose_reply_anchor(group, "嗯嗯。") is None

    monkeypatch.setattr(
        "mochi.llm.get_client_for_tier",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down")),
    )
    assert await choose_reply_anchor(group, "随便") is None


@pytest.mark.asyncio
async def test_choose_reply_anchor_empty_group():
    group = TopicGroup(texts=(), user_msg_ids=())
    assert await choose_reply_anchor(group, "hi") is None


@pytest.mark.asyncio
async def test_choose_reply_anchor_skips_lite_for_single_message(monkeypatch):
    group = TopicGroup(texts=("只有一句",), user_msg_ids=(42,))
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("lite should not run for single message")

    monkeypatch.setattr("mochi.llm.get_client_for_tier", boom)
    assert await choose_reply_anchor(group, "嗯") is None
    assert called["n"] == 0
