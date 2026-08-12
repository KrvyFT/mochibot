"""Essential autonomous Main behavior."""

from datetime import datetime, timedelta, timezone

import pytest

from mochi.ai_client import chat
from mochi.core_store import replace_core
from mochi.db import get_recent_messages
from mochi.heartbeat_runtime import set_schedule_due
from mochi.main_runtime import MainRuntimeEntry
from tests.e2e.mock_llm import make_response


@pytest.mark.asyncio
async def test_free_time_stays_free_of_private_life_context(
    mock_llm_factory,
    monkeypatch,
):
    import mochi.ai_client as ai_client

    replace_core("CORE_MARKER", source="test")
    monkeypatch.setattr(
        ai_client,
        "_retrieve_memories_for_turn",
        lambda *args: pytest.fail("Free Time must not auto-recall"),
    )
    mock = mock_llm_factory([make_response("[SKIP]")])
    entry = MainRuntimeEntry.free_time(
        run_key="free_time:test",
        wake_reason="periodic",
        user_id=1,
        channel_id=100,
        transport="fake",
        claim_token="claim",
        lease_until="2099-01-01T00:00:00+00:00",
    )

    result = await chat(runtime_entry=entry)

    prompt = mock.call_log[0]["messages"][0]["content"]
    assert result.disposition == "skip"
    assert "CORE_MARKER" in prompt
    assert len(mock.call_log[0]["messages"]) == 1


@pytest.mark.asyncio
async def test_failed_proactive_delivery_reuses_prepared_result(
    mock_llm_factory,
    monkeypatch,
):
    import mochi.heartbeat as heartbeat
    import mochi.heartbeat_runtime as runtime
    import mochi.observers as observers

    clock = {"now": datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(runtime, "_utc_now", lambda: clock["now"])

    async def no_observer_change():
        return False

    monkeypatch.setattr(observers, "collect_attention_facts", no_observer_change)
    mock = mock_llm_factory([make_response("I was thinking of you.")])
    deliveries = [False, True]
    prepared = 0

    async def prepare(entry):
        nonlocal prepared
        prepared += 1
        return await chat(runtime_entry=entry)

    async def deliver(_channel_id, _result):
        return deliveries.pop(0)

    heartbeat.set_main_runtime_callbacks(prepare, deliver, "fake")
    set_schedule_due("free_time", clock["now"])
    await heartbeat.run_main_runtime_tick(1, now=clock["now"])
    assert get_recent_messages(1) == []

    clock["now"] += timedelta(seconds=61)
    await heartbeat.run_main_runtime_tick(1, now=clock["now"])

    assert prepared == 1
    assert len(mock.call_log) == 1
    assert get_recent_messages(1)[0]["content"] == "I was thinking of you."
