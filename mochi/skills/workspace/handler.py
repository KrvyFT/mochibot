"""Workspace skill — diary read/write and markdown file editing."""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
import logging
import os
import re
from pathlib import Path
import tempfile

from mochi.diary import DiaryConflictError, diary
from mochi.skills.base import Skill, SkillContext, SkillResult

log = logging.getLogger(__name__)

_WEEKDAY_PREFIX = (
    r"(?:周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
)

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
_SYSTEM_PRIVATE_DIRS = frozenset({"prompts"})
_DIARY_PRIVATE_PATHS = frozenset({"diary.md"})
_DIARY_PRIVATE_DIRS = frozenset({"diary_archive"})
_MAX_LISTED_FILES = 100
_MAX_LIST_DEPTH = 4
_MAX_FILE_SNAPSHOTS = 64


@dataclass(frozen=True)
class _FileSnapshot:
    content: str | None


class _FileConflictError(ValueError):
    pass


class WorkspaceSkill(Skill):

    def __init__(self) -> None:
        super().__init__()
        self._file_snapshots: OrderedDict[
            tuple[int, str, str], _FileSnapshot
        ] = OrderedDict()

    async def execute(self, context: SkillContext) -> SkillResult:
        tool_name, args = context.tool_name, context.args
        if tool_name == "write_diary":
            return self._write_diary(args)
        elif tool_name == "read_diary":
            return SkillResult(output=self._read_diary(args))
        elif tool_name == "list_files":
            return SkillResult(output=self._list_files())
        elif tool_name == "edit_file":
            return self._edit_file(context)
        return SkillResult(output=f"Unknown tool: {tool_name}", success=False)

    def _write_diary(self, args: dict) -> SkillResult:
        content = args.get("content")
        expected = args.get("_expected_content")
        if not isinstance(content, str):
            return SkillResult(output="Error: content is required.", success=False)
        if not isinstance(expected, str):
            return SkillResult(
                output="Diary update context is unavailable. Try again next turn.",
                success=False,
            )
        lines = content.strip().splitlines()
        if lines and re.fullmatch(
            rf"#\s+Diary\s+{re.escape(diary.current_date())}"
            rf"\s+{_WEEKDAY_PREFIX}\s*[。.!：:]?",
            lines[0].strip(),
            flags=re.IGNORECASE,
        ):
            lines = lines[1:]
        content = "\n".join(lines).strip()
        try:
            result = diary.replace_section_exact(
                "今日日記",
                expected_content=expected,
                content=content,
            )
        except DiaryConflictError as exc:
            return SkillResult(
                output=(
                    f"Diary update rejected: {exc}\n\n"
                    f"Current journal:\n{diary.read(section='今日日記')}"
                ),
                success=False,
            )
        receipt = (
            f"Today's journal {'updated' if result['changed'] else 'unchanged'} "
            f"({result['chars']} chars)."
        )
        return SkillResult(
            output=receipt,
            summary=receipt,
            entity_refs=["diary:today"],
            state_changed=result["changed"],
        )

    def _read_diary(self, args: dict) -> str:
        date_str = (args.get("date") or "").strip()
        if not date_str:
            return diary.read(section="今日日記")

        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return "Error: date must use YYYY-MM-DD."
        if date_str == diary.current_date():
            return diary.read(section="今日日記")

        archive_path = (
            diary.path.parent / "diary_archive" / f"{date_str[:7]}.md"
        )
        if not archive_path.exists():
            return f"No diary archive found for {date_str[:7]}."
        raw = archive_path.read_text(encoding="utf-8")
        block = dict(diary._archive_blocks(raw)).get(date_str)
        if block is None:
            return f"No diary entry found for {date_str}."
        try:
            return diary._section_content(block, "今日日記")
        except ValueError:
            return f"No journal section found for {date_str}."

    @staticmethod
    def _private_path_reason(relative: Path) -> str | None:
        normalized_path = relative.as_posix().casefold()
        normalized_parts = tuple(part.casefold() for part in relative.parts)
        if (
            normalized_path in _CORE_PRIVATE_PATHS
            or (
                normalized_parts
                and normalized_parts[0] in _CORE_PRIVATE_DIRS
            )
        ):
            return (
                "Core storage is private; use update_core or the admin "
                "Core editor."
            )
        if (
            normalized_path in _DIARY_PRIVATE_PATHS
            or (
                normalized_parts
                and normalized_parts[0] in _DIARY_PRIVATE_DIRS
            )
        ):
            return "Diary storage is private; use read_diary or write_diary."
        if normalized_parts and normalized_parts[0] in _SYSTEM_PRIVATE_DIRS:
            return "Internal prompt storage is private."
        return None

    def _list_files(self) -> str:
        data_root = _DATA_DIR.resolve()
        if not data_root.exists():
            return "No Markdown files found under data/."

        paths: list[str] = []
        truncated = False
        for current_root, dirs, files in os.walk(data_root, followlinks=False):
            current = Path(current_root)
            relative_root = current.relative_to(data_root)
            depth = len(relative_root.parts)
            dirs[:] = sorted(
                directory
                for directory in dirs
                if depth < _MAX_LIST_DEPTH
                and self._private_path_reason(
                    relative_root / directory
                ) is None
            )
            for filename in sorted(files):
                if Path(filename).suffix.casefold() != ".md":
                    continue
                relative = relative_root / filename
                if self._private_path_reason(relative) is not None:
                    continue
                if len(paths) >= _MAX_LISTED_FILES:
                    truncated = True
                    break
                paths.append(relative.as_posix())
            if truncated:
                break

        if not paths:
            return "No Markdown files found under data/."
        output = "\n".join(paths)
        if truncated:
            output += f"\n... (showing first {_MAX_LISTED_FILES} files)"
        return output

    def _remember_snapshot(
        self,
        key: tuple[int, str, str],
        *,
        content: str | None,
    ) -> None:
        self._file_snapshots[key] = _FileSnapshot(content=content)
        self._file_snapshots.move_to_end(key)
        while len(self._file_snapshots) > _MAX_FILE_SNAPSHOTS:
            self._file_snapshots.popitem(last=False)

    def _snapshot_for_write(
        self,
        key: tuple[int, str, str],
    ) -> _FileSnapshot | None:
        snapshot = self._file_snapshots.get(key)
        if snapshot is not None:
            self._file_snapshots.move_to_end(key)
            return snapshot

        user_id, normalized_path, _ = key
        inherited = next(
            (
                candidate
                for candidate_key, candidate in reversed(
                    self._file_snapshots.items()
                )
                if candidate_key[:2] == (user_id, normalized_path)
            ),
            None,
        )
        if inherited is not None:
            self._remember_snapshot(key, content=inherited.content)
        return inherited

    @staticmethod
    def _read_file_content(target: Path) -> str | None:
        if not target.exists():
            return None
        return target.read_text(encoding="utf-8")

    @staticmethod
    def _replace_file(
        target: Path,
        *,
        expected_content: str | None,
        content: str,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp:
                temp_path = Path(temp.name)
                temp.write(content)
                temp.flush()
                os.fsync(temp.fileno())
            current = (
                target.read_text(encoding="utf-8")
                if target.exists()
                else None
            )
            if current != expected_content:
                raise _FileConflictError(
                    "File changed while the replacement was prepared."
                )
            os.replace(temp_path, target)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _edit_file(self, context: SkillContext) -> SkillResult:
        args = context.args
        action = (args.get("action") or "").lower()
        rel_path = (args.get("path") or "").strip()

        if not rel_path:
            return SkillResult(output="Error: path is required.", success=False)
        if Path(rel_path).suffix.casefold() != ".md":
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
        private_reason = self._private_path_reason(relative)
        if private_reason:
            return SkillResult(
                output=f"Error: {private_reason}",
                success=False,
            )
        key = (
            context.user_id,
            os.path.normcase(str(relative)),
            context.turn_id,
        )

        if action == "read":
            content = self._read_file_content(target)
            self._remember_snapshot(
                key,
                content=content,
            )
            if content is None:
                return SkillResult(
                    output=f"File not found: {relative.as_posix()}",
                    success=False,
                )
            return SkillResult(output=content)

        elif action == "write":
            content = args.get("content")
            if not isinstance(content, str):
                return SkillResult(
                    output="Error: content is required for write.",
                    success=False,
                )
            current = self._read_file_content(target)
            snapshot = self._snapshot_for_write(key)
            if snapshot is None:
                self._remember_snapshot(
                    key,
                    content=current,
                )
                current_text = current if current is not None else "(file missing)"
                return SkillResult(
                    output=(
                        "File write needs a current read snapshot; no write was "
                        f"applied.\n\nCurrent content:\n{current_text}"
                    ),
                    success=False,
                )
            if snapshot.content != current:
                self._remember_snapshot(
                    key,
                    content=current,
                )
                current_text = current if current is not None else "(file missing)"
                return SkillResult(
                    output=(
                        "File changed after it was read; no write was applied. "
                        "The snapshot is now refreshed.\n\n"
                        f"Current content:\n{current_text}"
                    ),
                    success=False,
                )

            changed = current != content
            if changed:
                try:
                    self._replace_file(
                        target,
                        expected_content=current,
                        content=content,
                    )
                except _FileConflictError:
                    refreshed = self._read_file_content(target)
                    self._remember_snapshot(
                        key,
                        content=refreshed,
                    )
                    current_text = (
                        refreshed if refreshed is not None else "(file missing)"
                    )
                    return SkillResult(
                        output=(
                            "File changed while the write was prepared; no "
                            "write was applied. The snapshot is now refreshed."
                            f"\n\nCurrent content:\n{current_text}"
                        ),
                        success=False,
                    )
                log.info(
                    "edit_file: wrote %s (%d chars)",
                    relative.as_posix(),
                    len(content),
                )
            self._remember_snapshot(
                key,
                content=content,
            )
            receipt = (
                f"OK: {relative.as_posix()} "
                f"{'written' if changed else 'unchanged'} ({len(content)} chars)."
            )
            return SkillResult(
                output=receipt,
                summary=receipt,
                entity_refs=[f"file:{relative.as_posix()}"],
                state_changed=changed,
            )

        return SkillResult(
            output=f"Error: unknown action '{action}'. Use read or write.",
            success=False,
        )
