"""High-signal autonomous Main contracts."""

from datetime import datetime, timezone

import pytest

from mochi.ai_client import chat
from mochi.db import save_message
from mochi.heartbeat_runtime import (
    get_unresolved_attention_facts,
    sync_attention_facts,
)
from mochi.main_runtime import MainRuntimeEntry
from mochi.skills.weather.observer import WeatherObserver
from tests.e2e.mock_llm import make_response


@pytest.mark.asyncio
async def test_weather_is_context_only_and_legacy_attention_is_retired(
    monkeypatch,
):
    import mochi.observers as observers

    observed_at = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)
    sync_attention_facts(
        "weather",
        [{
            "stable_key": "current_conditions",
            "facts": {"summary": "Suzhou: 35 C, Sunny"},
        }],
        observed_at=observed_at,
        freshness_seconds=7200,
    )
    observer = WeatherObserver()
    observer.meta.enabled = False
    monkeypatch.setattr(observers, "_observers", {"weather": observer})

    assert observer.has_delta(
        {"temperature_c": 20},
        {"temperature_c": 35},
    ) is False
    assert observer.attention_facts({"summary": "Suzhou: 35 C, Sunny"}) == []

    await observers.collect_attention_facts()

    assert get_unresolved_attention_facts(now=observed_at) == ()


@pytest.mark.asyncio
async def test_free_time_uses_recent_complete_turns_and_consumes_skip_marker(
    mock_llm_factory,
):
    for number in range(3):
        turn_id = f"history-{number}"
        save_message(1, "user", f"user-{number}", turn_id=turn_id)
        save_message(1, "assistant", f"assistant-{number}", turn_id=turn_id)
    save_message(1, "user", "stale orphan", turn_id="incomplete")
    mock = mock_llm_factory([
        make_response("[SKIP] 等等，我还是想说一句。"),
    ])
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

    history = [
        item
        for item in mock.call_log[0]["messages"][1:]
        if item["role"] in {"user", "assistant"}
    ]
    assert [item["content"].split("] ", 1)[-1] for item in history] == [
        "user-1",
        "assistant-1",
        "user-2",
        "assistant-2",
    ]
    assert result.text == "等等，我还是想说一句。"
    assert result.disposition == "deliver"
