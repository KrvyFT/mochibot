"""Workspace skill — diary read/write and markdown file editing."""

import logging
from pathlib import Path

from mochi.diary import diary
from mochi.skills.base import Skill, SkillContext, SkillResult

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
_CORE_PRIVATE_PATHS = frozenset({
    "core.md",
    "notes.md",
    ".core.lock",
    "core_migration.json",
    "notes_retirement.json",
})
_CORE_PRIVATE_DIRS = frozenset({
    "core_history",
    "core_migration_backup",
    "notes_retirement_backup",
    "core_weekly_receipts",
})


class WorkspaceSkill(Skill):

    async def execute(self, context: SkillContext) -> SkillResult:
        tool_name, args = context.tool_name, context.args
        if tool_name == "write_diary":
            return self._write_diary(args)
        elif tool_name == "read_diary":
            return self._read_diary(args)
        elif tool_name == "edit_file":
            return self._edit_file(args)
        return SkillResult(output=f"Unknown tool: {tool_name}", success=False)

    def _write_diary(self, args: dict) -> SkillResult:
        entry = (args.get("entry") or "").strip()
        if not entry:
            return SkillResult(output="Error: entry is required.", success=False)
        before = diary.read_raw()
        output = diary.append(entry, source="chat", section="今日日記")
        return SkillResult(
            output=output,
            state_changed=diary.read_raw() != before,
        )

    def _read_diary(self, args: dict) -> SkillResult:
        date_str = (args.get("date") or "").strip()
        if not date_str:
            content = diary.read_raw()
            return SkillResult(
                output=content if content else "Today's diary is empty."
            )

        try:
            year_month = date_str[:7]
            archive_dir = diary.path.parent / "diary_archive"
            archive_path = archive_dir / f"{year_month}.md"
            if not archive_path.exists():
                return SkillResult(
                    output=f"No diary archive found for {year_month}.",
                    success=False,
                )

            raw = archive_path.read_text(encoding="utf-8")
            lines = raw.split("\n")
            collecting = False
            result: list[str] = []
            for line in lines:
                if line.startswith("# Diary ") and date_str in line:
                    collecting = True
                    result.append(line)
                elif collecting and line.startswith("# Diary "):
                    break
                elif collecting:
                    result.append(line)

            if not result:
                return SkillResult(
                    output=f"No diary entry found for {date_str}.",
                    success=False,
                )
            return SkillResult(output="\n".join(result).strip())
        except Exception as e:
            return SkillResult(
                output=f"Error reading diary archive: {e}",
                success=False,
            )

    def _edit_file(self, args: dict) -> SkillResult:
        action = (args.get("action") or "").lower()
        rel_path = (args.get("path") or "").strip()

        if not rel_path:
            return SkillResult(output="Error: path is required.", success=False)
        if not rel_path.endswith(".md"):
            return SkillResult(
                output="Error: only .md files are supported.",
                success=False,
            )

        data_root = _DATA_DIR.resolve()
        target = (data_root / rel_path).resolve()
        if not target.is_relative_to(data_root):
            return SkillResult(
                output="Error: path must be within data/ directory.",
                success=False,
            )
        relative = target.relative_to(data_root)
        normalized_path = relative.as_posix().casefold()
        normalized_parts = tuple(part.casefold() for part in relative.parts)
        if (
            normalized_path in _CORE_PRIVATE_PATHS
            or (
                normalized_parts
                and normalized_parts[0] in _CORE_PRIVATE_DIRS
            )
        ):
            return SkillResult(
                output=(
                    "Error: Core storage is private; use update_core or "
                    "the admin Core editor."
                ),
                success=False,
            )

        if action == "read":
            if not target.exists():
                return SkillResult(
                    output=f"File not found: {rel_path}",
                    success=False,
                )
            return SkillResult(output=target.read_text(encoding="utf-8"))

        elif action == "write":
            content = args.get("content")
            if content is None:
                return SkillResult(
                    output="Error: content is required for write.",
                    success=False,
                )
            previous = (
                target.read_text(encoding="utf-8")
                if target.exists()
                else None
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".md.tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(target)
            log.info("edit_file: wrote %s (%d chars)", rel_path, len(content))
            return SkillResult(
                output=f"OK: {rel_path} written ({len(content)} chars).",
                state_changed=previous != content,
            )

        return SkillResult(
            output=f"Error: unknown action '{action}'. Use read or write.",
            success=False,
        )
