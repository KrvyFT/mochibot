"""Telegram outbound retry helper."""

import asyncio

import pytest

from mochi.transport.tg_send import call_telegram_api


@pytest.mark.asyncio
async def test_call_telegram_api_succeeds_within_quick_retries():
    attempts = {"n": 0}

    async def factory():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TimeoutError("slow")
        return True

    ok = await call_telegram_api(
        factory,
        label="test",
        timeout_s=0.05,
        quick_retries=3,
        max_attempts=5,
        max_backoff_s=0.05,
    )
    assert ok is True
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_call_telegram_api_keeps_retrying_past_quick_then_caps_in_tests():
    attempts = {"n": 0}

    async def factory():
        attempts["n"] += 1
        raise TimeoutError("always")

    ok = await call_telegram_api(
        factory,
        label="test",
        timeout_s=0.01,
        quick_retries=2,
        max_attempts=4,
        max_backoff_s=0.01,
    )
    assert ok is False
    assert attempts["n"] == 4


@pytest.mark.asyncio
async def test_photo_send_uses_hard_attempt_cap(monkeypatch):
    """Photo TimedOut must not retry forever (duplicate uploads)."""
    from pathlib import Path
    import tempfile

    from mochi.transport import telegram as tg

    class _Bot:
        def __init__(self):
            self.n = 0

        async def send_photo(self, **kwargs):
            self.n += 1
            raise TimeoutError("Timed out")

    class _App:
        def __init__(self, bot):
            self.bot = bot

    bot = _Bot()
    transport = tg.TelegramTransport.__new__(tg.TelegramTransport)
    transport._app = _App(bot)

    monkeypatch.setattr("mochi.config.TG_PHOTO_SEND_TIMEOUT_S", 0.01)
    monkeypatch.setattr("mochi.config.TG_PHOTO_SEND_MAX_ATTEMPTS", 2)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "a.jpg"
        path.write_bytes(b"\xff\xd8\xff\xd9")
        ok = await transport.send_photo_file(1, str(path))
    assert ok is False
    assert bot.n == 2
