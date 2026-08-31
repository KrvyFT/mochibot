"""Workspace containment contract."""

from datetime import datetime, timezone

import pytest

import mochi.diary as diary_module
from mochi.diary import DailyFile
from mochi.skills.base import SkillContext


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    import mochi.skills.workspace.handler as workspace_module
    from mochi.skills.workspace.handler import WorkspaceSkill

    diary = DailyFile(
        path=tmp_path / "diary.md",
        label="Diary",
        max_lines=50,
        sections=("今日状態", "今日日記"),
        section_max_lines={"今日状態": 20, "今日日記": 30},
    )
    monkeypatch.setattr(diary_module, "TZ", timezone.utc)
    logical_day = {"value": "2025-06-15"}
    monkeypatch.setattr(
        diary_module,
        "_diary_date",
        lambda: datetime.fromisoformat(
            f"{logical_day['value']}T10:00:00+00:00"
        ),
    )
    monkeypatch.setattr(
        diary_module,
        "_today_str",
        lambda: logical_day["value"],
    )
    monkeypatch.setattr(diary_module, "_now_time", lambda: "10:00")
    monkeypatch.setattr(workspace_module, "diary", diary)
    monkeypatch.setattr(workspace_module, "_DATA_DIR", tmp_path)
    skill = WorkspaceSkill()
    skill._name = "workspace"
    return skill, tmp_path, diary, logical_day


def _context(tool_name, args):
    return SkillContext(
        trigger="tool_call",
        user_id=1,
        turn_id="turn-1",
        tool_name=tool_name,
        args=args,
    )


@pytest.mark.asyncio
async def test_workspace_rejects_private_and_outside_paths(workspace):
    skill, root, diary, logical_day = workspace
    outside = root.with_name(f"{root.name}_outside")
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("must stay private")

    traversal = await skill.execute(_context(
        "edit_file", {"action": "read", "path": "../../secret.md"},
    ))
    sibling = await skill.execute(_context(
        "edit_file", {"action": "read", "path": str(secret)},
    ))
    private = await skill.execute(_context(
        "edit_file",
        {"action": "write", "path": "core.md", "content": "nope"},
    ))
    diary_private = await skill.execute(_context(
        "edit_file",
        {"action": "read", "path": "diary_archive/2025-06.md"},
    ))
    tomorrow_private = await skill.execute(_context(
        "edit_file",
        {"action": "read", "path": "diary_tomorrow.json"},
    ))
    prompt_private = await skill.execute(_context(
        "edit_file",
        {"action": "read", "path": "prompts/system.md"},
    ))

    assert "Error" in traversal.output
    assert "Error" in sibling.output
    assert "must stay private" not in sibling.output
    assert "Core storage is private" in private.output
    assert "Diary storage is private" in diary_private.output
    assert "Diary storage is private" in tomorrow_private.output
    assert "Internal prompt storage is private" in prompt_private.output

    today = await skill.execute(_context(
        "write_diary",
        {
            "content": "Today wrapped up.",
            "day": "today",
            "_expected_content": "",
            "_source_date": "2025-06-15",
            "_target_date": "2025-06-15",
        },
    ))
    tomorrow = await skill.execute(_context(
        "write_diary",
        {
            "content": "Keep watching the unfinished thing.",
            "day": "tomorrow",
            "_expected_content": "",
            "_source_date": "2025-06-15",
            "_target_date": "2025-06-16",
        },
    ))
    conflicting = await skill.execute(_context(
        "write_diary",
        {
            "content": "Overwrite without the current draft.",
            "day": "tomorrow",
            "_expected_content": "",
            "_source_date": "2025-06-15",
            "_target_date": "2025-06-16",
        },
    ))
    assert today.success and tomorrow.success
    assert not conflicting.success
    assert diary.read(section="今日日記") == "Today wrapped up."

    logical_day["value"] = "2025-06-16"
    stale_tomorrow = await skill.execute(_context(
        "write_diary",
        {
            "content": "Must not retarget after rollover.",
            "day": "tomorrow",
            "_expected_content": "Keep watching the unfinished thing.",
            "_source_date": "2025-06-15",
            "_target_date": "2025-06-16",
        },
    ))
    stale_today = await skill.execute(_context(
        "write_diary",
        {
            "content": "Must not land on June 16.",
            "day": "today",
            "_expected_content": "Today wrapped up.",
            "_source_date": "2025-06-15",
            "_target_date": "2025-06-15",
        },
    ))
    assert not stale_tomorrow.success
    assert not stale_today.success
    carried = diary.read(section="今日日記")
    assert "来自 2025-06-15 睡前，留给今天" in carried
    assert "Keep watching the unfinished thing." in carried
    assert diary.read(section="今日日記").count("留给今天") == 1
    assert not diary.tomorrow_draft_path.exists()

    snapshot_required = await skill.execute(_context(
        "write_diary",
        {
            "content": "This requires seeing the existing draft.",
            "day": "tomorrow",
            "_expected_content": None,
            "_source_date": "2025-06-16",
            "_target_date": "2025-06-17",
        },
    ))
    assert not snapshot_required.success
    assert "Current journal:" in snapshot_required.output

    next_draft = await skill.execute(_context(
        "write_diary",
        {
            "content": "This belongs only to June 17.",
            "day": "tomorrow",
            "_expected_content": "",
            "_source_date": "2025-06-16",
            "_target_date": "2025-06-17",
        },
    ))
    assert next_draft.success
    logical_day["value"] = "2025-06-18"
    assert "This belongs only to June 17." not in diary.read(
        section="今日日記",
    )
    archive = root / "diary_archive" / "2025-06.md"
    archived = archive.read_text(encoding="utf-8")
    assert "# Diary 2025-06-17" in archived
    assert "来自 2025-06-16 睡前，留给今天" in archived
    assert "This belongs only to June 17." in archived
