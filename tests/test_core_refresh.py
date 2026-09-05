"""Scheduled Core refresh hours and once-per-slot claiming."""

import asyncio
from datetime import datetime, timezone

import mochi.heartbeat as heartbeat

UTC = timezone.utc


def test_parse_core_refresh_hours_defaults():
    assert heartbeat.parse_core_refresh_hours("") == (12, 23)
    assert heartbeat.parse_core_refresh_hours("12,23") == (12, 23)
    assert heartbeat.parse_core_refresh_hours("9, 21, 9") == (9, 21)


def test_scheduled_hour_reached_before_and_after_noon():
    mh = 3
    before = datetime(2026, 9, 2, 11, 59, tzinfo=UTC)
    noon = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    night = datetime(2026, 9, 2, 23, 0, tzinfo=UTC)
    after_midnight = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)
    assert not heartbeat.scheduled_hour_reached(before, 12, maintenance_hour=mh)
    assert heartbeat.scheduled_hour_reached(noon, 12, maintenance_hour=mh)
    assert not heartbeat.scheduled_hour_reached(noon, 23, maintenance_hour=mh)
    assert heartbeat.scheduled_hour_reached(night, 12, maintenance_hour=mh)
    assert heartbeat.scheduled_hour_reached(night, 23, maintenance_hour=mh)
    # 01:00 still belongs to the previous logical day (before MAINTENANCE_HOUR).
    assert heartbeat.scheduled_hour_reached(
        after_midnight, 23, maintenance_hour=mh,
    )


def test_core_refresh_claims_noon_once(monkeypatch):
    calls: list[str] = []

    async def cb(user_id, logical_date, period_key):
        calls.append(period_key)

    monkeypatch.setattr(heartbeat, "_core_refresh_callback", cb)
    monkeypatch.setattr(heartbeat, "_core_refresh_busy", False)

    def _effective(key: str):
        return {
            "CORE_REFRESH_ENABLED": True,
            "CORE_REFRESH_HOURS": "12,23",
            "MAINTENANCE_HOUR": 3,
            "LLM_HEARTBEAT_TIMEOUT_SECONDS": 5,
        }[key]

    monkeypatch.setattr(heartbeat, "_effective", _effective)
    now = datetime(2026, 9, 2, 12, 1, tzinfo=UTC)

    async def body():
        assert await heartbeat._run_core_refresh_if_due(1, now)
        assert calls == ["2026-09-02-12"]
        calls.clear()
        assert not await heartbeat._run_core_refresh_if_due(1, now)
        assert calls == []

    asyncio.run(body())


def test_core_refresh_entry_period_key():
    from mochi.main_runtime import MainRuntimeEntry

    entry = MainRuntimeEntry.core_refresh(
        logical_date="2026-09-02",
        period_key="2026-09-02-12",
        user_id=1,
        channel_id=1,
        transport="telegram",
    )
    assert entry.kind == "core_refresh"
    assert entry.idempotency_key == "core-refresh:1:2026-09-02-12"
    assert entry.is_last_refresh_of_day is False


def test_is_last_core_refresh_of_day():
    hours = (12, 23)
    assert not heartbeat.is_last_core_refresh_of_day("2026-09-02-12", hours)
    assert heartbeat.is_last_core_refresh_of_day("2026-09-02-23", hours)
    assert not heartbeat.is_last_core_refresh_of_day("force-20260902T120000", hours)
    assert heartbeat.core_refresh_hour_from_period_key("2026-09-02-23") == 23
    assert heartbeat.core_refresh_hour_from_period_key("force-x") is None


def test_last_refresh_entry_flag():
    from mochi.main_runtime import MainRuntimeEntry

    entry = MainRuntimeEntry.core_refresh(
        logical_date="2026-09-02",
        period_key="2026-09-02-23",
        user_id=1,
        channel_id=1,
        transport="telegram",
        is_last_refresh_of_day=True,
    )
    assert entry.is_last_refresh_of_day is True
