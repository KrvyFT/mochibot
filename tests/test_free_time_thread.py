"""Unanswered Free Time thread: count, excerpts, and situation injection."""

from mochi.ai_client import (
    _render_autonomous_situation,
    _render_completed_conversation_evidence,
    _render_unanswered_free_time_thread,
    _unanswered_free_time_guidance,
)
from mochi.db import get_unanswered_free_time_thread, save_message
from mochi.main_runtime import MainRuntimeEntry


def _entry(*, direct_search: bool = False) -> MainRuntimeEntry:
    return MainRuntimeEntry.free_time(
        run_key="free_time:test:0",
        wake_reason="daily_random",
        user_id=1,
        channel_id=1,
        transport="telegram",
        claim_token="token",
        lease_until="2026-09-01T12:00:00+00:00",
        direct_search=direct_search,
    )


def test_ordinary_free_time_asks_the_owner_not_to_monologue():
    text = _render_autonomous_situation(_entry(direct_search=False))
    assert "找对方说话" in text
    assert "不要自言自语" in text or "不要一个人在路边念叨" in text
    assert "send_photo" in text
    assert "真实" in text
    assert "随便做点什么" not in text
    assert "先搜一件" not in text


def test_free_time_situation_requires_first_daily_photo(monkeypatch):
    monkeypatch.setattr(
        "mochi.admin.admin_db.is_draw_tier_ready", lambda: True,
    )
    text = _render_autonomous_situation(_entry(direct_search=False))
    assert "还没发过照片" in text
    assert "send_photo" in text
    assert "出图失败" in text


def test_free_time_situation_includes_intimacy_guidance(monkeypatch):
    monkeypatch.setattr(
        "mochi.ai_client._free_time_intimacy_guidance",
        lambda entry: "禁止主动「好想你」",
    )
    text = _render_autonomous_situation(_entry(direct_search=False))
    assert "禁止主动「好想你」" in text


def test_search_free_time_shares_with_the_owner():
    text = _render_autonomous_situation(_entry(direct_search=True))
    assert "先搜一件" in text
    assert "递到他眼前" in text
    assert "自己看完不说" in text


def test_situation_injects_unanswered_thread_and_continue_guidance():
    thread = {
        "count": 1,
        "items": [{"content": "你在干嘛呀", "created_at": "t", "turn_id": "free_time:x"}],
    }
    text = _render_autonomous_situation(_entry(), unanswered_thread=thread)
    assert "unanswered_free_time_thread" in text
    assert "count: 1" in text
    assert "你在干嘛呀" in text
    assert "必须接上" in text


def test_guidance_escalates_hurt_then_heat():
    assert "不要自言自语" in _unanswered_free_time_guidance(0)
    assert "必须接上" in _unanswered_free_time_guidance(1)
    assert "失落" in _unanswered_free_time_guidance(2)
    assert "闷气" in _unanswered_free_time_guidance(3)


def test_user_reply_clears_unanswered_free_time_thread():
    save_message(1, "user", "在的", turn_id="chat-1")
    save_message(
        1, "assistant", "你在干嘛呀",
        turn_id="free_time:2026-09-01:0", processed=True,
    )
    save_message(1, "user", "刚回来", turn_id="chat-2")
    thread = get_unanswered_free_time_thread(1)
    assert thread["count"] == 0
    assert thread["items"] == []


def test_counts_free_time_after_last_user_message():
    save_message(1, "user", "先走了", turn_id="chat-1")
    save_message(
        1, "assistant", "那我去数石子",
        turn_id="free_time:2026-09-01:0", processed=True,
    )
    save_message(
        1, "assistant", "还没回来吗",
        turn_id="free_time:2026-09-01:1", processed=True,
    )
    save_message(
        1, "assistant", "普通回复",
        turn_id="uuid-not-free-time", processed=True,
    )
    thread = get_unanswered_free_time_thread(1)
    assert thread["count"] == 2
    assert [item["content"] for item in thread["items"]] == [
        "那我去数石子",
        "还没回来吗",
    ]


def test_count_is_not_capped_when_excerpts_are():
    save_message(1, "user", "嗯", turn_id="chat-1")
    for index in range(6):
        save_message(
            1, "assistant", f"第{index}次",
            turn_id=f"free_time:2026-09-01:{index}", processed=True,
        )
    thread = get_unanswered_free_time_thread(1, limit=2)
    assert thread["count"] == 6
    assert [item["content"] for item in thread["items"]] == ["第4次", "第5次"]


def test_evidence_preface_tells_free_time_to_continue_outreach():
    history = [{
        "role": "assistant",
        "content": "你在干嘛呀",
        "processed": True,
    }]
    closed = _render_completed_conversation_evidence(history)
    assert "不是仍待延续的话头" in closed
    open_thread = _render_completed_conversation_evidence(
        history, continue_unanswered_outreach=True,
    )
    assert "尚未被接上的 Free Time 话头" in open_thread
    assert "不要当成已经结束的独白" in open_thread


def test_thread_renderer_truncates_long_previous_lines():
    block = _render_unanswered_free_time_thread({
        "count": 1,
        "items": [{"content": "啊" * 250}],
    })
    assert "…" in block
    assert len([line for line in block.splitlines() if line.startswith("- ")]) == 1
