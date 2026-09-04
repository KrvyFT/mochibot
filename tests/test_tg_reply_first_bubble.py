"""First outbound bubble replies; later bubbles are plain sends."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import mochi.transport.telegram as tg


@pytest.mark.asyncio
async def test_send_message_replies_only_on_first_bubble(monkeypatch):
    transport = tg.TelegramTransport()
    calls: list[dict] = []

    async def fake_send_message(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(message_id=len(calls))

    async def fake_chat_action(**_kwargs):
        return None

    bot = SimpleNamespace(
        send_message=fake_send_message,
        send_chat_action=fake_chat_action,
    )
    transport._app = SimpleNamespace(bot=bot)
    monkeypatch.setattr(tg, "TG_BUBBLE_DELAY_S", 0)
    monkeypatch.setattr(tg, "_split_bubbles", lambda text, *_a: ["第一句", "第二句", "第三句"])

    async def immediate(factory, **_kwargs):
        await factory()
        return True

    monkeypatch.setattr(tg, "call_telegram_api", immediate)

    ok = await transport.send_message(1, "ignored", reply_to_message_id=99)
    assert ok is True
    assert len(calls) == 3
    assert calls[0].get("reply_to_message_id") == 99
    assert "reply_to_message_id" not in calls[1]
    assert "reply_to_message_id" not in calls[2]
