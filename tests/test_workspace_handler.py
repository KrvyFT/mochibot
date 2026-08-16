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


def _context(tool_name, args):
    return SkillContext(
        trigger="tool_call", user_id=1, tool_name=tool_name, args=args
    )


@pytest.mark.asyncio
async def test_diary_write_can_be_read_back(workspace):
    skill, diary, _ = workspace

    write = await skill.execute(_context("write_diary", {"entry": "went to gym"}))
    read = await skill.execute(_context("read_diary", {}))

    assert write.success
    assert "went to gym" in read.output
    assert "[10:00]" in diary.read_raw()


@pytest.mark.asyncio
async def test_markdown_file_write_can_be_read_back(workspace):
    skill, _, root = workspace

    write = await skill.execute(_context(
        "edit_file", {"action": "write", "path": "draft.md", "content": "hello"}
    ))
    read = await skill.execute(_context(
        "edit_file", {"action": "read", "path": "draft.md"}
    ))

    assert write.success
    assert read.output == "hello"
    assert (root / "draft.md").read_text() == "hello"


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

    assert "Error" in traversal.output
    assert "Error" in sibling.output
    assert "must stay private" not in sibling.output
    assert "Core storage is private" in private.output
