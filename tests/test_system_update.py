"""Owner-requested self-update handoff contract."""

import pytest

from mochi.ai_client import ChatResult
from mochi.skills.base import SkillContext
from mochi.update_service import ReleaseInfo


@pytest.mark.asyncio
async def test_install_is_armed_only_after_reply_delivery(monkeypatch):
    import mochi.shutdown as shutdown
    import mochi.skills.system_update.handler as handler

    release = ReleaseInfo(
        tag="v1.1.0",
        version="1.1.0",
        name="v1.1.0",
        notes="",
        url="https://github.com/shikidmsh-rgb/mochibot/releases/tag/v1.1.0",
        current_version="1.0.2",
        available=True,
    )

    async def fake_check():
        return release

    staged = []
    exits = []
    monkeypatch.setattr(handler, "check_for_update", fake_check)
    monkeypatch.setattr(
        handler,
        "stage_update",
        lambda value, **context: staged.append((value, context)),
    )
    monkeypatch.setattr(
        shutdown,
        "request_process_exit",
        lambda code: exits.append(code),
    )

    skill = handler.SystemUpdateSkill()
    result = await skill.execute(SkillContext(
        trigger="tool_call",
        user_id=1,
        channel_id=99,
        transport="telegram",
        actor="main",
        source="chat",
        turn_id="turn-1",
        tool_name="install_system_update",
        args={},
    ))

    assert result.success
    assert staged[0][0] == release
    assert staged[0][1]["channel_id"] == 99
    assert exits == []

    delivered = ChatResult(
        text="马上更新。",
        _after_delivery=[result.after_delivery],
    )
    assert delivered.confirm_delivered()
    assert exits == [shutdown.UPDATE_EXIT_CODE]

    autonomous = await skill.execute(SkillContext(
        trigger="tool_call",
        user_id=1,
        actor="main",
        source="runtime:attention",
        tool_name="install_system_update",
        args={},
    ))
    assert not autonomous.success
