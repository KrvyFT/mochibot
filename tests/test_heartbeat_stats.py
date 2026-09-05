"""Heartbeat get_stats exposes delivery / search / photo counters."""

from mochi.heartbeat import get_stats


def test_get_stats_includes_delivery_quotas(monkeypatch):
    monkeypatch.setattr("mochi.config.OWNER_USER_ID", 1)
    monkeypatch.setattr(
        "mochi.heartbeat_runtime.count_delivered_search_shares",
        lambda *_a, **_k: 1,
    )
    monkeypatch.setattr(
        "mochi.skills.photo.quota.today_photo_count",
        lambda *_a, **_k: 0,
    )
    stats = get_stats()
    assert "free_time_delivered_today" in stats
    assert "free_time_failed_today" in stats
    assert stats["free_time_search_today"] == 1
    assert stats["free_time_search_min"] == 2
    assert stats["free_time_photo_today"] == 0
    assert stats["free_time_photo_min"] == 1
    assert stats["free_time_photo_max"] == 3
    assert stats["plan_date"]
