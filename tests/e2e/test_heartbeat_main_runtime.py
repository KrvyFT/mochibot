"""High-signal contracts for the unified Free Time runtime."""

import random
from datetime import datetime, timezone

import pytest

from mochi.ai_client import chat
from mochi.db import _connect, save_message
from mochi.free_time import FreeTimeCard
from mochi.heartbeat_runtime import ensure_daily_free_time_plan
from mochi.main_runtime import MainRuntimeEntry
from tests.e2e.mock_llm import make_response


def test_daily_plan_never_exceeds_the_single_limit():
    now = datetime(2026, 8, 24, 5, 0, tzinfo=timezone.utc)

    ensure_daily_free_time_plan(
        user_id=1,
        channel_id=100,
        transport="fake",
        now=now,
        max_daily=6,
        rng=random.Random(7),
    )

    conn = _connect()
    count = conn.execute(
        "SELECT COUNT(*) FROM heartbeat_runs WHERE entry_kind = 'free_time'"
    ).fetchone()[0]
    attention_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'attention_facts'"
    ).fetchone()
    conn.close()
    assert count <= 6
    assert attention_table is None


@pytest.mark.asyncio
async def test_free_time_card_is_read_only_context_with_its_skill_available(
    mock_llm_factory,
):
    save_message(
        1,
        "assistant",
        "Earlier outreach",
        turn_id="free_time:earlier",
        processed=True,
    )
    mock = mock_llm_factory([make_response("背单词了吗？")])
    card = FreeTimeCard(
        source="habit",
        stable_key="habit:2",
        capability_skill="habit",
        facts={
            "habit_id": 2,
            "name": "每天背单词",
            "progress": "0/1",
            "period": "2026-08-24",
            "context": "",
            "importance": "normal",
        },
    )
    entry = MainRuntimeEntry.free_time(
        run_key="free_time:2026-08-24:1:test",
        wake_reason="random_slot",
        user_id=1,
        channel_id=100,
        transport="fake",
        claim_token="claim",
        lease_until="2099-01-01T00:00:00+00:00",
        card=card,
    )

    result = await chat(runtime_entry=entry)

    messages = mock.call_log[0]["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "Earlier outreach" in messages[0]["content"]
    assert "<current_life_fact>" in messages[1]["content"]
    assert "每天背单词" in messages[1]["content"]
    assert mock.call_log[0]["tools"] is None
    assert result.text == "背单词了吗？"
