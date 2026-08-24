"""High-signal autonomous Main contracts."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from mochi.ai_client import chat
from mochi.db import _connect, save_message
from mochi.heartbeat_runtime import (
    get_schedulable_runs,
    get_unresolved_attention_facts,
    materialize_due_runs,
    set_schedule_due,
    sync_attention_facts,
)
from mochi.main_runtime import MainRuntimeEntry
from mochi.skills.weather.observer import WeatherObserver
from tests.e2e.mock_llm import make_response


def test_periodic_attention_runs_without_observer_facts():
    due = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)
    set_schedule_due("attention", due, wake_reason="periodic")

    created = materialize_due_runs(
        user_id=1,
        channel_id=100,
        transport="fake",
        now=due,
        attention_interval_minutes=60,
        free_time_min_minutes=90,
        free_time_max_minutes=240,
    )

    assert created == [f"attention:{due.isoformat()}"]
    runs = get_schedulable_runs(now=due)
    assert len(runs) == 1
    assert runs[0]["entry_kind"] == "attention"
    assert runs[0]["wake_reason"] == "periodic"
    assert runs[0]["facts_json"] == "[]"


def test_periodic_attention_defers_new_facts_behind_unfinished_run():
    first_due = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)
    set_schedule_due("attention", first_due, wake_reason="periodic")
    first = materialize_due_runs(
        user_id=1,
        channel_id=100,
        transport="fake",
        now=first_due,
        attention_interval_minutes=60,
        free_time_min_minutes=90,
        free_time_max_minutes=240,
    )
    second_due = first_due + timedelta(hours=1)
    sync_attention_facts(
        "test",
        [{"stable_key": "new-fact", "facts": {"value": "noticed"}}],
        observed_at=second_due,
        freshness_seconds=3600,
    )
    set_schedule_due("attention", second_due, wake_reason="observer_change")
    assert materialize_due_runs(
        user_id=1,
        channel_id=100,
        transport="fake",
        now=second_due,
        attention_interval_minutes=60,
        free_time_min_minutes=90,
        free_time_max_minutes=240,
    ) == []
    runs = get_schedulable_runs(now=second_due)
    assert [run["run_key"] for run in runs] == first
    assert runs[0]["facts_json"] == "[]"
    conn = _connect()
    deferred = conn.execute(
        "SELECT run_key, status, facts_json, wake_reason "
        "FROM heartbeat_runs WHERE status = 'deferred'"
    ).fetchone()
    conn.close()
    facts = json.loads(deferred["facts_json"])
    assert deferred["run_key"] == f"attention:{second_due.isoformat()}"
    assert deferred["wake_reason"] == "observer_change"
    assert facts[0]["stable_key"] == "new-fact"
    assert facts[0]["facts"] == {"value": "noticed"}


def test_periodic_attention_does_not_defer_unchanged_facts():
    first_due = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)
    sync_attention_facts(
        "test",
        [{"stable_key": "same-fact", "facts": {"value": "same"}}],
        observed_at=first_due,
        freshness_seconds=7200,
    )
    set_schedule_due("attention", first_due, wake_reason="observer_change")
    first = materialize_due_runs(
        user_id=1,
        channel_id=100,
        transport="fake",
        now=first_due,
        attention_interval_minutes=60,
        free_time_min_minutes=90,
        free_time_max_minutes=240,
    )

    second_due = first_due + timedelta(minutes=30)
    set_schedule_due("attention", second_due, wake_reason="periodic")
    assert materialize_due_runs(
        user_id=1,
        channel_id=100,
        transport="fake",
        now=second_due,
        attention_interval_minutes=60,
        free_time_min_minutes=90,
        free_time_max_minutes=240,
    ) == []
    conn = _connect()
    deferred_count = conn.execute(
        "SELECT COUNT(*) FROM heartbeat_runs WHERE status = 'deferred'"
    ).fetchone()[0]
    conn.close()
    assert deferred_count == 0
    assert [run["run_key"] for run in get_schedulable_runs(now=second_due)] == first


def test_attention_fact_metadata_refresh_is_not_a_canonical_change():
    first_due = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)
    sync_attention_facts(
        "test",
        [{"stable_key": "same-fact", "facts": {"value": "same"}}],
        observed_at=first_due,
        freshness_seconds=7200,
    )
    set_schedule_due("attention", first_due, wake_reason="observer_change")
    materialize_due_runs(
        user_id=1,
        channel_id=100,
        transport="fake",
        now=first_due,
        attention_interval_minutes=60,
        free_time_min_minutes=90,
        free_time_max_minutes=240,
    )
    refreshed = first_due + timedelta(minutes=10)
    sync_attention_facts(
        "test",
        [{"stable_key": "same-fact", "facts": {"value": "same"}}],
        observed_at=refreshed,
        freshness_seconds=7200,
    )
    set_schedule_due("attention", refreshed, wake_reason="observer_change")

    materialize_due_runs(
        user_id=1,
        channel_id=100,
        transport="fake",
        now=refreshed,
        attention_interval_minutes=60,
        free_time_min_minutes=90,
        free_time_max_minutes=240,
    )

    conn = _connect()
    deferred_count = conn.execute(
        "SELECT COUNT(*) FROM heartbeat_runs WHERE status = 'deferred'"
    ).fetchone()[0]
    conn.close()
    assert deferred_count == 0


def test_legacy_attention_backlog_is_serialized():
    conn = _connect()
    for number in range(3):
        conn.execute(
            "INSERT INTO heartbeat_runs "
            "(run_key, entry_kind, user_id, channel_id, transport, wake_reason, "
            "facts_json, status, created_at) "
            "VALUES (?, 'attention', 1, 100, 'fake', 'periodic', '[]', "
            "'pending', ?)",
            (
                f"attention:legacy-{number}",
                f"2026-08-24T0{number}:00:00+00:00",
            ),
        )
    conn.commit()
    conn.close()

    runs = get_schedulable_runs(
        now=datetime(2026, 8, 24, 4, 0, tzinfo=timezone.utc),
    )

    assert [run["run_key"] for run in runs] == ["attention:legacy-0"]


def test_oldest_attention_backoff_blocks_later_legacy_runs():
    now = datetime(2026, 8, 24, 4, 0, tzinfo=timezone.utc)
    conn = _connect()
    conn.execute(
        "INSERT INTO heartbeat_runs "
        "(run_key, entry_kind, user_id, channel_id, transport, wake_reason, "
        "facts_json, status, next_attempt_at, created_at) "
        "VALUES ('attention:oldest', 'attention', 1, 100, 'fake', "
        "'periodic', '[]', 'pending', ?, '2026-08-24T01:00:00+00:00')",
        ((now + timedelta(hours=1)).isoformat(),),
    )
    conn.execute(
        "INSERT INTO heartbeat_runs "
        "(run_key, entry_kind, user_id, channel_id, transport, wake_reason, "
        "facts_json, status, created_at) "
        "VALUES ('attention:later', 'attention', 1, 100, 'fake', "
        "'periodic', '[]', 'pending', '2026-08-24T02:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    assert get_schedulable_runs(now=now) == []


def test_claim_quarantines_malformed_attention_facts():
    now = datetime(2026, 8, 24, 4, 0, tzinfo=timezone.utc)
    conn = _connect()
    conn.execute(
        "INSERT INTO heartbeat_runs "
        "(run_key, entry_kind, user_id, channel_id, transport, wake_reason, "
        "facts_json, status, created_at) "
        "VALUES ('attention:broken', 'attention', 1, 100, 'fake', "
        "'periodic', '{broken', 'pending', ?)",
        (now.isoformat(),),
    )
    conn.commit()
    conn.close()

    from mochi.heartbeat_runtime import claim_run

    assert claim_run("attention:broken", now=now) is None
    conn = _connect()
    row = conn.execute(
        "SELECT status, outcome, last_error FROM heartbeat_runs "
        "WHERE run_key = 'attention:broken'"
    ).fetchone()
    conn.close()
    assert dict(row) == {
        "status": "delivered",
        "outcome": "invalid",
        "last_error": "invalid attention facts_json",
    }


@pytest.mark.parametrize(
    "payload",
    [
        [{"source": 1, "stable_key": "x", "observed_at": "2026-08-24T00:00:00+00:00",
          "freshness": "fresh", "status": "unresolved", "facts": {}}],
        [{"source": "x", "stable_key": "x", "observed_at": "bad",
          "freshness": "fresh", "status": "unresolved", "facts": {}}],
        [{"source": "x", "stable_key": "x", "observed_at": "2026-08-24T00:00:00+00:00",
          "freshness": "new", "status": "unresolved", "facts": {}}],
        [{"source": "x", "stable_key": "x", "observed_at": "2026-08-24T00:00:00+00:00",
          "freshness": "fresh", "status": "resolved", "facts": {}}],
        [{"source": "x", "stable_key": "x", "observed_at": "2026-08-24T00:00:00+00:00",
          "freshness": "fresh", "status": "unresolved", "facts": "bad"}],
    ],
)
def test_claim_quarantines_invalid_attention_fact_fields(payload):
    now = datetime(2026, 8, 24, 4, 0, tzinfo=timezone.utc)
    conn = _connect()
    conn.execute(
        "INSERT INTO heartbeat_runs "
        "(run_key, entry_kind, user_id, channel_id, transport, wake_reason, "
        "facts_json, status, created_at) "
        "VALUES ('attention:invalid-fields', 'attention', 1, 100, 'fake', "
        "'periodic', ?, 'pending', ?)",
        (json.dumps(payload), now.isoformat()),
    )
    conn.commit()
    conn.close()

    from mochi.heartbeat_runtime import claim_run

    assert claim_run("attention:invalid-fields", now=now) is None


def test_deferred_attention_drops_resolved_facts_before_promotion():
    first_due = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)
    set_schedule_due("attention", first_due, wake_reason="periodic")
    first = materialize_due_runs(
        user_id=1,
        channel_id=100,
        transport="fake",
        now=first_due,
        attention_interval_minutes=60,
        free_time_min_minutes=90,
        free_time_max_minutes=240,
    )
    second_due = first_due + timedelta(hours=1)
    sync_attention_facts(
        "test",
        [{"stable_key": "temporary", "facts": {"value": "present"}}],
        observed_at=second_due,
        freshness_seconds=7200,
    )
    set_schedule_due("attention", second_due, wake_reason="observer_change")
    materialize_due_runs(
        user_id=1,
        channel_id=100,
        transport="fake",
        now=second_due,
        attention_interval_minutes=60,
        free_time_min_minutes=90,
        free_time_max_minutes=240,
    )
    sync_attention_facts(
        "test",
        [],
        observed_at=second_due + timedelta(minutes=10),
        freshness_seconds=7200,
    )
    conn = _connect()
    conn.execute(
        "UPDATE heartbeat_runs SET status = 'delivered' WHERE run_key = ?",
        (first[0],),
    )
    conn.commit()
    conn.close()
    third_due = second_due + timedelta(hours=1)
    set_schedule_due("attention", third_due, wake_reason="periodic")

    promoted = materialize_due_runs(
        user_id=1,
        channel_id=100,
        transport="fake",
        now=third_due,
        attention_interval_minutes=60,
        free_time_min_minutes=90,
        free_time_max_minutes=240,
    )

    runs = get_schedulable_runs(now=third_due)
    assert [run["run_key"] for run in runs] == promoted
    assert runs[0]["facts_json"] == "[]"


def test_attention_promotes_one_coalesced_dirty_snapshot_after_terminal():
    first_due = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)
    set_schedule_due("attention", first_due, wake_reason="periodic")
    first = materialize_due_runs(
        user_id=1,
        channel_id=100,
        transport="fake",
        now=first_due,
        attention_interval_minutes=60,
        free_time_min_minutes=90,
        free_time_max_minutes=240,
    )
    conn = _connect()
    conn.execute(
        "UPDATE heartbeat_runs SET status = 'ready', result_json = '{}' "
        "WHERE run_key = ?",
        (first[0],),
    )
    conn.commit()
    conn.close()

    second_due = first_due + timedelta(hours=1)
    sync_attention_facts(
        "test",
        [{"stable_key": "fact-1", "facts": {"value": 1}}],
        observed_at=second_due,
        freshness_seconds=7200,
    )
    set_schedule_due("attention", second_due, wake_reason="observer_change")
    second = materialize_due_runs(
        user_id=1,
        channel_id=100,
        transport="fake",
        now=second_due,
        attention_interval_minutes=60,
        free_time_min_minutes=90,
        free_time_max_minutes=240,
    )
    assert second == []
    assert [run["run_key"] for run in get_schedulable_runs(now=second_due)] == first

    third_due = second_due + timedelta(hours=1)
    sync_attention_facts(
        "test",
        [
            {"stable_key": "fact-1", "facts": {"value": 1}},
            {"stable_key": "fact-2", "facts": {"value": 2}},
        ],
        observed_at=third_due,
        freshness_seconds=7200,
    )
    set_schedule_due("attention", third_due, wake_reason="observer_change")
    assert materialize_due_runs(
        user_id=1,
        channel_id=100,
        transport="fake",
        now=third_due,
        attention_interval_minutes=60,
        free_time_min_minutes=90,
        free_time_max_minutes=240,
    ) == []
    conn = _connect()
    queued = conn.execute(
        "SELECT run_key, status, facts_json FROM heartbeat_runs "
        "WHERE status = 'deferred'",
    ).fetchone()
    conn.execute(
        "UPDATE heartbeat_runs SET status = 'delivered' WHERE run_key = ?",
        (first[0],),
    )
    conn.commit()
    conn.close()
    assert {
        item["stable_key"] for item in json.loads(queued["facts_json"])
    } == {"fact-1", "fact-2"}
    assert queued["status"] == "deferred"

    fourth_due = third_due + timedelta(hours=1)
    set_schedule_due("attention", fourth_due, wake_reason="periodic")
    assert materialize_due_runs(
        user_id=1,
        channel_id=100,
        transport="fake",
        now=fourth_due,
        attention_interval_minutes=60,
        free_time_min_minutes=90,
        free_time_max_minutes=240,
    ) == [queued["run_key"]]
    promoted = get_schedulable_runs(now=fourth_due)
    assert [run["run_key"] for run in promoted] == [queued["run_key"]]
    assert {
        item["stable_key"] for item in json.loads(promoted[0]["facts_json"])
    } == {"fact-1", "fact-2"}


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
