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
