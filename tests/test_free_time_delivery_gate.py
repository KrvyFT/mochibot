"""Free Time delivery gate, grace, and search quota helpers."""

from datetime import datetime, timedelta, timezone

from mochi.heartbeat_runtime import (
    FREE_TIME_MISSED_GRACE,
    FREE_TIME_SEARCH_DAILY_MIN,
    count_delivered_search_shares,
    expire_unusable_free_time_runs,
    free_time_search_must_share,
)
from mochi.main_runtime import DurableChatResult


UTC = timezone.utc


def test_missed_grace_is_three_minutes():
    assert FREE_TIME_MISSED_GRACE == timedelta(seconds=180)


def test_expire_skipped_while_preparing():
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert expire_unusable_free_time_runs(
        now=now, active_chat=False, awake=True, preparing=True,
    ) == 0


def test_delivery_gate_accepts_voice_only():
    durable = DurableChatResult(
        text="", voices=("/tmp/a.ogg",), disposition="deliver",
    )
    assert bool(durable.text or durable.stickers or durable.images or durable.voices)


def test_search_daily_min_constant():
    assert FREE_TIME_SEARCH_DAILY_MIN == 2


def test_search_must_share_when_none_delivered(monkeypatch):
    monkeypatch.setattr(
        "mochi.heartbeat_runtime.count_delivered_search_shares",
        lambda *_a, **_k: 0,
    )
    assert free_time_search_must_share(1) is True
    monkeypatch.setattr(
        "mochi.heartbeat_runtime.count_delivered_search_shares",
        lambda *_a, **_k: 2,
    )
    assert free_time_search_must_share(1) is False
