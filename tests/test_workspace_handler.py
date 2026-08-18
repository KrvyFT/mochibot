"""Real file operations and containment checks for the workspace skill."""

from datetime import datetime, timezone

import pytest

import mochi.diary as diary_module
from mochi.diary import DailyFile
from mochi.skills.base import SkillContext


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from mochi.skills.workspace.handler import WorkspaceSkill
    import mochi.skills.workspace.handler as workspace_module

    diary = DailyFile(
        path=tmp_path / "diary.md",
        label="Diary",
        max_lines=50,
        sections=("今日状態", "今日日記"),
        section_max_lines={"今日状態": 20, "今日日記": 30},
    )
    monkeypatch.setattr(diary_module, "TZ", timezone.utc)
    monkeypatch.setattr(
        diary_module, "_diary_date",
        lambda: datetime(2025, 6, 15, 10, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(diary_module, "_today_str", lambda: "2025-06-15")
    monkeypatch.setattr(diary_module, "_now_time", lambda: "10:00")
    monkeypatch.setattr(workspace_module, "diary", diary)
    monkeypatch.setattr(workspace_module, "_DATA_DIR", tmp_path)
    skill = WorkspaceSkill()
    skill._name = "workspace"
    return skill, diary, tmp_path


def _context(tool_name, args, *, turn_id="turn-1"):
    return SkillContext(
        trigger="tool_call",
        user_id=1,
        turn_id=turn_id,
        tool_name=tool_name,
        args=args,
    )


@pytest.mark.asyncio
async def test_diary_write_can_be_read_back(workspace):
    skill, diary, _ = workspace
    detail = "went to gym and kept the complete story " + ("x" * 140)
    existing = diary.read(section="今日日記")

    write = await skill.execute(_context(
        "write_diary",
        {
            "content": (
                "# Diary 2025-06-15 Sunday\n"
                "2025-06-15 was the deadline, and I finished.\n\n"
                f"## Trip notes\n{detail}"
            ),
            "_expected_content": existing,
        },
    ))
    read = await skill.execute(_context("read_diary", {}))

    assert write.success
    assert write.state_changed
    assert detail in read.output
    assert "2025-06-15 Sunday" not in read.output
    assert "2025-06-15 was the deadline" in read.output
    assert "## Trip notes\n" in read.output
    assert "..." not in read.output
    assert "[10:00]" not in diary.read_raw()

    diary.rewrite_section("今日状態", ["- current status"])
    assert "## Trip notes\n" in diary.read(section="今日日記")

    cleared = await skill.execute(_context(
        "write_diary",
        {
            "content": "",
            "_expected_content": diary.read(section="今日日記"),
        },
    ))
    assert cleared.state_changed
    assert (await skill.execute(_context("read_diary", {}))).output == ""

    date_body = await skill.execute(_context(
        "write_diary",
        {
            "content": "2025-06-15\nThis date is part of the story.",
            "_expected_content": "",
        },
    ))
    assert date_body.state_changed
    assert diary.read(section="今日日記").startswith("2025-06-15\n")

    archive = diary.path.parent / "diary_archive" / "2025-06.md"
    archive.parent.mkdir()
    archive.write_text(
        "# Diary 2025-06-14 Saturday\n\n"
        "## 今日状態\n"
        "- private status\n\n"
        "## 今日日記\n"
        "A quiet day.\n\n"
        "## A free-form subtitle\n"
        "This remains part of the journal.\n",
        encoding="utf-8",
    )
    historical = await skill.execute(_context(
        "read_diary", {"date": "2025-06-14"},
    ))
    explicit_today = await skill.execute(_context(
        "read_diary", {"date": "2025-06-15"},
    ))

    assert historical.output == (
        "A quiet day.\n\n"
        "## A free-form subtitle\n"
        "This remains part of the journal."
    )
    assert "private status" not in historical.output
    assert "# Diary" not in historical.output
    assert explicit_today.output == diary.read(section="今日日記")


@pytest.mark.asyncio
async def test_markdown_file_write_can_be_read_back(workspace, monkeypatch):
    skill, _, root = workspace

    unread_write = await skill.execute(_context(
        "edit_file", {"action": "write", "path": "draft.md", "content": "hello"}
    ))
    write = await skill.execute(_context(
        "edit_file", {"action": "write", "path": "draft.md", "content": "hello"}
    ))
    read = await skill.execute(_context(
        "edit_file", {"action": "read", "path": "draft.md"}
    ))

    assert unread_write.success
    assert not unread_write.state_changed
    assert "current read snapshot" in unread_write.output
    assert write.success
    assert write.state_changed
    assert read.output == "hello"
    assert (root / "draft.md").read_text() == "hello"

    (root / "draft.md").write_text("changed elsewhere", encoding="utf-8")
    conflict = await skill.execute(_context(
        "edit_file",
        {"action": "write", "path": "draft.md", "content": "new version"},
        turn_id="turn-2",
    ))
    retry = await skill.execute(_context(
        "edit_file",
        {"action": "write", "path": "draft.md", "content": "new version"},
        turn_id="turn-2",
    ))

    assert conflict.success
    assert not conflict.state_changed
    assert "changed after it was read" in conflict.output
    assert "changed elsewhere" in conflict.output
    assert retry.success
    assert (root / "draft.md").read_text(encoding="utf-8") == "new version"

    (root / "shared.md").write_text("v1", encoding="utf-8")
    await skill.execute(_context(
        "edit_file",
        {"action": "read", "path": "shared.md"},
        turn_id="turn-a",
    ))
    await skill.execute(_context(
        "edit_file",
        {"action": "read", "path": "shared.md"},
        turn_id="turn-b",
    ))
    first_writer = await skill.execute(_context(
        "edit_file",
        {"action": "write", "path": "shared.md", "content": "v2"},
        turn_id="turn-a",
    ))
    stale_writer = await skill.execute(_context(
        "edit_file",
        {"action": "write", "path": "shared.md", "content": "v3"},
        turn_id="turn-b",
    ))

    assert first_writer.success
    assert stale_writer.success
    assert not stale_writer.state_changed
    assert (root / "shared.md").read_text(encoding="utf-8") == "v2"

    followup = await skill.execute(_context(
        "edit_file",
        {"action": "write", "path": "shared.md", "content": "v4"},
        turn_id="turn-c",
    ))
    assert followup.success
    assert (root / "shared.md").read_text(encoding="utf-8") == "v4"

    (root / "locked.md").write_text("before", encoding="utf-8")
    await skill.execute(_context(
        "edit_file",
        {"action": "read", "path": "locked.md"},
        turn_id="locked-turn",
    ))

    def fail_replace(*_args, **_kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr(skill, "_replace_file", fail_replace)
    locked = await skill.execute(_context(
        "edit_file",
        {"action": "write", "path": "locked.md", "content": "after"},
        turn_id="locked-turn",
    ))
    assert not locked.success
    assert "File write failed" in locked.output


@pytest.mark.asyncio
async def test_workspace_rejects_private_and_outside_paths(workspace):
    skill, _, root = workspace
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
        "edit_file", {"action": "write", "path": "core.md", "content": "nope"},
    ))
    diary_private = await skill.execute(_context(
        "edit_file", {"action": "read", "path": "diary_archive/2025-06.md"},
    ))
    prompt_private = await skill.execute(_context(
        "edit_file", {"action": "read", "path": "prompts/system.md"},
    ))

    assert "Error" in traversal.output
    assert "Error" in sibling.output
    assert "must stay private" not in sibling.output
    assert "Core storage is private" in private.output
    assert "Diary storage is private" in diary_private.output
    assert "Internal prompt storage is private" in prompt_private.output
