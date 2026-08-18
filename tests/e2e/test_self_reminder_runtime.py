"""One durable Self Reminder delivery path."""

from datetime import datetime, timedelta, timezone

import pytest

from mochi.ai_client import ChatResult, chat
from mochi.db import (
    _connect,
    finish_tool_execution,
    get_recent_messages,
    start_tool_execution,
)
from mochi.reminder_timer import (
    _fire_reminder,
    set_self_reminder_callbacks,
    set_send_callback,
)
from mochi.skills.reminder.queries import (
    create_reminder,
    create_self_reminder,
    get_schedulable_reminders,
)
from tests.e2e.mock_llm import make_response


@pytest.mark.asyncio
async def test_failed_delivery_reuses_prepared_main_result(
    mock_llm_factory,
    monkeypatch,
):
    import mochi.reminder_timer as timer
    import mochi.skills.reminder.queries as queries

    clock = {"now": datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(timer, "_utc_now", lambda: clock["now"])
    monkeypatch.setattr(queries, "_now", lambda: clock["now"])
    create_self_reminder(
        1,
        100,
        "check whether to bring an umbrella",
        (clock["now"] - timedelta(minutes=1)).isoformat(),
        "fake",
    )
    mock = mock_llm_factory([make_response("Remember your umbrella tomorrow.")])
    deliveries = [False, True]
    prepared = 0

    async def prepare(entry):
        nonlocal prepared
        prepared += 1
        return await chat(runtime_entry=entry)

    async def deliver(_channel_id, _result):
        return deliveries.pop(0)

    set_self_reminder_callbacks(prepare, deliver, "fake")
    await _fire_reminder(get_schedulable_reminders(now=clock["now"])[0])
    assert get_recent_messages(1) == []

    clock["now"] += timedelta(seconds=61)
    await _fire_reminder(get_schedulable_reminders(now=clock["now"])[0])

    assert prepared == 1
    assert len(mock.call_log) == 1
    assert get_recent_messages(1)[0]["content"] == (
        "Remember your umbrella tomorrow."
    )


@pytest.mark.asyncio
async def test_recurring_notify_advances_same_row(monkeypatch):
    import mochi.reminder_timer as timer
    import mochi.skills.reminder.queries as queries

    clock = {"now": datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(timer, "_utc_now", lambda: clock["now"])
    monkeypatch.setattr(queries, "_now", lambda: clock["now"])

    due = clock["now"] - timedelta(days=3, minutes=1)
    reminder_id = create_reminder(
        1,
        100,
        "drink water",
        due.isoformat(),
        "daily",
    )

    async def rephrase(_message, _user_id):
        return "Drink water."

    delivered = []

    async def send(_user_id, text):
        delivered.append(text)
        return True

    monkeypatch.setattr(timer, "_rephrase_reminder", rephrase)
    set_send_callback(send)
    await _fire_reminder(get_schedulable_reminders(now=clock["now"])[0])

    conn = _connect()
    row = conn.execute(
        "SELECT id, status, remind_at, recurrence, prepared_text "
        "FROM reminders WHERE id = ?",
        (reminder_id,),
    ).fetchone()
    conn.close()
    assert delivered == ["Drink water."]
    assert row["status"] == "pending"
    expected_next = due + timedelta(days=4)
    assert row["remind_at"] == expected_next.isoformat()
    assert datetime.fromisoformat(row["remind_at"]) > clock["now"]
    assert row["recurrence"] == "daily"
    assert row["prepared_text"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        ChatResult(disposition="skip"),
        ChatResult(successful_effects=True, disposition="handled"),
    ],
)
async def test_recurring_self_advances_after_silent_outcome(
    monkeypatch,
    result,
):
    import mochi.reminder_timer as timer
    import mochi.skills.reminder.queries as queries

    clock = {"now": datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(timer, "_utc_now", lambda: clock["now"])
    monkeypatch.setattr(queries, "_now", lambda: clock["now"])
    due = clock["now"] - timedelta(minutes=1)
    reminder_id = create_self_reminder(
        1,
        100,
        "review hydration progress",
        due.isoformat(),
        "fake",
        "daily",
    )

    async def prepare(_entry):
        return result

    async def deliver(_channel_id, _result):
        raise AssertionError("silent outcome must not cross transport")

    set_self_reminder_callbacks(prepare, deliver, "fake")
    await _fire_reminder(get_schedulable_reminders(now=clock["now"])[0])

    conn = _connect()
    row = conn.execute(
        "SELECT status, remind_at, recurrence, result_json, outcome "
        "FROM reminders WHERE id = ?",
        (reminder_id,),
    ).fetchone()
    conn.close()
    assert row["status"] == "pending"
    assert row["remind_at"] == (due + timedelta(days=1)).isoformat()
    assert row["recurrence"] == "daily"
    assert row["result_json"] is None
    assert row["outcome"] is None


@pytest.mark.asyncio
async def test_recurring_self_recovery_advances_without_replaying_main(
    monkeypatch,
):
    import mochi.reminder_timer as timer
    import mochi.skills.reminder.queries as queries

    clock = {"now": datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(timer, "_utc_now", lambda: clock["now"])
    monkeypatch.setattr(queries, "_now", lambda: clock["now"])
    due = clock["now"] - timedelta(minutes=1)
    reminder_id = create_self_reminder(
        1,
        100,
        "review hydration progress",
        due.isoformat(),
        "fake",
        "daily",
    )
    execution_id = start_tool_execution(
        turn_id=f"self-reminder:{reminder_id}:{due.isoformat()}",
        tool_call_id="call-1",
        user_id=1,
        source="runtime:self_reminder",
        skill_name="habit",
        tool_name="query_habit",
        action="list",
        arguments_json='{"action":"list"}',
    )
    finish_tool_execution(
        execution_id,
        status="success",
        state_changed=False,
    )
    prepared = 0

    async def prepare(_entry):
        nonlocal prepared
        prepared += 1
        return ChatResult(text="should not run")

    set_self_reminder_callbacks(prepare, None, "fake")
    await _fire_reminder(get_schedulable_reminders(now=clock["now"])[0])

    conn = _connect()
    row = conn.execute(
        "SELECT status, remind_at FROM reminders WHERE id = ?",
        (reminder_id,),
    ).fetchone()
    conn.close()
    assert prepared == 0
    assert row["status"] == "pending"
    assert row["remind_at"] == (due + timedelta(days=1)).isoformat()
