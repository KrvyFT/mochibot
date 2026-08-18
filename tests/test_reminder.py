import pytest

from mochi.db import _connect
from mochi.skills.base import SkillContext
from mochi.skills.reminder.handler import ReminderSkill


def _context(user_id: int, **args) -> SkillContext:
    return SkillContext(
        trigger="tool_call",
        user_id=user_id,
        channel_id=user_id,
        transport="wechat",
        actor="main",
        source="chat",
        turn_id="turn-reminder",
        tool_name="manage_reminder",
        args=args,
    )


@pytest.mark.asyncio
async def test_recurring_reminders_can_be_listed_updated_and_owned_by_user_zero(
    monkeypatch,
):
    import mochi.config as config

    monkeypatch.setattr(config, "OWNER_USER_ID", None)
    skill = ReminderSkill()

    notify = await skill.execute(_context(
        0,
        action="create",
        kind="notify",
        message="drink water",
        remind_at="2026-08-19T08:30:00+08:00",
        recurrence="daily",
    ))
    assert notify.success
    assert "repeats daily" in notify.output

    self_reminder = await skill.execute(_context(
        0,
        action="create",
        kind="self",
        intent="look at today's hydration progress",
        remind_at="2026-08-19T20:00:00+08:00",
        recurrence="daily",
    ))
    assert self_reminder.success
    assert "repeats daily" in self_reminder.output

    listed = await skill.execute(_context(0, action="list"))
    assert listed.success
    assert listed.output.count("（daily）") == 2

    notify_id = int(notify.entity_refs[0].split(":")[1])
    updated = await skill.execute(_context(
        0,
        action="update",
        reminder_id=notify_id,
        recurrence="one_time",
    ))
    assert updated.success
    assert "repeats daily" not in updated.output

    conn = _connect()
    rows = {
        row["kind"]: dict(row)
        for row in conn.execute(
            "SELECT kind, recurrence FROM reminders ORDER BY id"
        )
    }
    conn.close()
    assert rows["notify"]["recurrence"] is None
    assert rows["self"]["recurrence"] == "daily"


@pytest.mark.asyncio
@pytest.mark.parametrize("recurrence", ["hourly", "monthly"])
async def test_invalid_recurrence_is_rejected(recurrence):
    result = await ReminderSkill().execute(_context(
        1,
        action="create",
        kind="notify",
        message="test",
        remind_at="2026-08-19T08:30:00+08:00",
        recurrence=recurrence,
    ))

    assert not result.success
    assert "Invalid recurrence" in result.output
