"""Outgoing replies must not start with history timestamps."""

from mochi.ai_client import _clean_model_reply
from mochi.transport.utils import (
    split_bubbles,
    strip_outgoing_history_timestamps,
    strip_stage_directions,
)


def test_strips_prefix_from_sent_reply():
    text = "[2026-09-02 22:01] 我自己都没数清楚说了些什么"
    assert strip_outgoing_history_timestamps(text) == "我自己都没数清楚说了些什么"


def test_keeps_mid_sentence_timestamp():
    text = "会议定在 [2026-09-02 22:01] 开始"
    assert strip_outgoing_history_timestamps(text) == text


def test_strips_each_outgoing_bubble():
    text = (
        "[2026-09-02 22:01] 第一句\n\n"
        "[2026-09-02 22:01] 第二句"
    )
    bubbles = split_bubbles(text)
    assert bubbles == ["第一句", "第二句"]


def test_clean_model_reply_drops_copied_prefixes():
    reply = (
        "[2026-09-02 22:01] 我自己都没数清楚说了些什么，"
        "你倒替我理得明明白白"
    )
    assert not _clean_model_reply(reply).startswith("[2026-09-02")
    assert "我自己都没数清楚" in _clean_model_reply(reply)


def test_clean_model_reply_drops_parenthetical_stage_directions():
    reply = (
        "（缓了半拍，声音落下来，轻轻软软地递过去）\n"
        "风把我的声音顺过来了。"
    )
    cleaned = _clean_model_reply(reply)
    assert "缓了半拍" not in cleaned
    assert cleaned == "风把我的声音顺过来了。"
    assert strip_stage_directions("ok (fine)") == "ok (fine)"
