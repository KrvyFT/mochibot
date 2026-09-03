"""Telegram visitor admission and ephemeral session isolation."""

from mochi.ai_client import ChatResult
from mochi.transport.telegram import classify_telegram_sender
from mochi.visitor_session import (
    append_visitor_turn,
    clear_visitor_sessions,
    visitor_history,
)


def test_classify_telegram_sender(monkeypatch):
    import mochi.admin.admin_db as admin_db
    import mochi.config as config

    monkeypatch.setattr(config, "OWNER_USER_ID", 0)
    monkeypatch.setattr(
        admin_db, "get_system_config",
        lambda key: True if key == "TELEGRAM_ALLOW_VISITORS" else None,
    )
    assert classify_telegram_sender(99, "private") == "claim"

    monkeypatch.setattr(config, "OWNER_USER_ID", 7)
    assert classify_telegram_sender(7, "private") == "owner"
    assert classify_telegram_sender(99, "private") == "visitor"
    assert classify_telegram_sender(99, "group") == "reject"
    assert classify_telegram_sender(99, "supergroup") == "reject"

    monkeypatch.setattr(
        admin_db, "get_system_config",
        lambda key: False if key == "TELEGRAM_ALLOW_VISITORS" else None,
    )
    assert classify_telegram_sender(99, "private") == "reject"


def test_visitor_session_is_ephemeral():
    clear_visitor_sessions()
    append_visitor_turn(99, "你好", "嗯，我在。")
    history = visitor_history(99)
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "你好"
    assert history[1]["role"] == "assistant"
    clear_visitor_sessions()
    assert visitor_history(99) == []


def test_visitor_delivery_does_not_save_owner_history(monkeypatch):
    clear_visitor_sessions()
    saved = []

    def _save(*args, **kwargs):
        saved.append((args, kwargs))
        return True

    monkeypatch.setattr("mochi.ai_client.save_message_once", _save)
    result = ChatResult(
        text="嗯。",
        _pending_history={
            "visitor": True,
            "user_id": 99,
            "user_content": "嗨",
            "content": "嗯。",
            "tool_history": None,
            "turn_id": "t1",
            "processed": False,
        },
    )
    assert result.confirm_delivered()
    assert saved == []
    history = visitor_history(99)
    assert [item["content"] for item in history] == ["嗨", "嗯。"]
    clear_visitor_sessions()


def test_visitor_prompt_uses_owner_core_and_guest_contract():
    from mochi.ai_client import _build_system_prompt

    prompt = _build_system_prompt(
        99,
        core_memory="Core：主人喜欢绿茶。",
        is_visitor=True,
    )
    assert "主人喜欢绿茶" in prompt
    assert "临时访客" in prompt
    assert "不要把访客当成主人" in prompt


def test_owner_prompt_does_not_include_visitor_contract():
    from mochi.ai_client import _build_system_prompt

    prompt = _build_system_prompt(1, core_memory="Core：主人喜欢绿茶。")
    assert "临时访客" not in prompt
