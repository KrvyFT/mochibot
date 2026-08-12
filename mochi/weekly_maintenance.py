"""Bounded context and entry-scoped tools for silent Weekly Main."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from mochi import config
from mochi.db import (
    get_recent_user_messages_in_window,
)
from mochi.core_store import (
    CoreError,
    has_weekly_core_update,
    update_weekly_core_exact,
)
from mochi.diary import DiaryArchiveWindow, read_diary_archive_window
from mochi.memory_curation import (
    MemoryCurationError,
    WeeklyMemoryCandidate,
    WeeklyMemoryCandidatePackage,
    build_weekly_memory_candidate_package,
    curate_memory_items,
    get_weekly_curation_receipt,
    set_weekly_projection_state,
)
from mochi.skills.base import SkillResult


log = logging.getLogger(__name__)

CORE_TOOL = "update_weekly_core"
CURATE_TOOL = "curate_weekly_memory"

_CORE_DEFINITION = {
    "type": "function",
    "function": {
        "name": CORE_TOOL,
        "description": (
            "Apply exact edit/delete/insert_after patches to the free-text Core "
            "only when the complete visible snapshot is still current. Preserve "
            "the user's document organization. This can succeed once per week."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expected_content": {"type": "string"},
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {"type": "object"},
                },
            },
            "required": ["expected_content", "operations"],
            "additionalProperties": False,
        },
    },
}

_CURATE_DEFINITION = {
    "type": "function",
    "function": {
        "name": CURATE_TOOL,
        "description": (
            "Atomically create, edit, merge, or archive only the visible "
            "Weekly Memory candidates. Each operation must be one of: "
            "create(op,content,category,importance,evidence_message_ids); "
            "edit(op,item_id,expected_content,expected_updated_at,content,"
            "category,importance,evidence_message_ids); "
            "merge(op,keep:{item_id,expected_content,expected_updated_at},"
            "remove:[same shape],content,category,importance,"
            "evidence_message_ids); archive(op,item_id,expected_content,"
            "expected_updated_at,evidence_message_ids). "
            "Use at most one successful batch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {"type": "object"},
                },
            },
            "required": ["operations"],
            "additionalProperties": False,
        },
    },
}


class WeeklyProjectionError(RuntimeError):
    """A committed Weekly curation still needs its derived KG projection."""


@dataclass(frozen=True)
class WeeklyMaintenanceContext:
    logical_date: str
    period_key: str
    window_start: datetime
    window_end: datetime
    diary: DiaryArchiveWindow
    package: WeeklyMemoryCandidatePackage
    recent_user_messages: tuple[dict, ...]
    allowed_item_ids: frozenset[int]
    allowed_evidence_message_ids: frozenset[int]
    rendered: str


def _render_candidate(item: WeeklyMemoryCandidate) -> str:
    lines = [
        (
            f"- item_id={item.id} category={item.category} "
            f"importance={item.importance} source={item.source} "
            f"created_at={item.created_at} updated_at={item.updated_at}"
        ),
        f"  content: {item.content}",
    ]
    if item.evidence_excerpts:
        lines.append("  visible evidence:")
        lines.extend(
            (
                f"    - message_id={excerpt.message_id} "
                f"created_at={excerpt.created_at}: {excerpt.content}"
            )
            for excerpt in item.evidence_excerpts
        )
    else:
        lines.append("  visible evidence: none")
    return "\n".join(lines)


def _render_context(
    logical_date: str,
    diary: DiaryArchiveWindow,
    package: WeeklyMemoryCandidatePackage,
    recent_messages: list[dict],
) -> str:
    window_items = (
        "\n".join(_render_candidate(item) for item in package.window_items)
        or "(none)"
    )
    related_items = (
        "\n".join(_render_candidate(item) for item in package.related_items)
        or "(none)"
    )
    recent = "\n".join(
        (
            f"- message_id={message['id']} created_at={message['created_at']}: "
            f"{message['content']}"
        )
        for message in recent_messages
    ) or "(none)"
    return (
        "## Weekly bounded context\n"
        f"logical_monday: {logical_date}\n"
        f"window: [{package.window_start}, {package.window_end})\n\n"
        "### Archived Diary (read-only)\n"
        f"available_dates: {', '.join(diary.dates) if diary.dates else 'none'}\n"
        f"total_chars: {diary.total_chars}\n"
        f"truncated: {str(diary.truncated).lower()}\n"
        f"{diary.content or '(none)'}\n\n"
        "### Window Memory candidates\n"
        f"window_total: {package.window_total}\n"
        f"rendered_count: {len(package.window_items)}\n"
        f"truncated: {str(package.window_truncated).lower()}\n"
        f"{window_items}\n\n"
        "### Related older Memory candidates\n"
        f"related_eligible_total: {package.related_eligible_total}\n"
        f"rendered_count: {len(package.related_items)}\n"
        f"truncated: {str(package.related_truncated).lower()}\n"
        f"{related_items}\n\n"
        "### Recent user messages available as additional evidence\n"
        f"{recent}\n\n"
        "Only rendered item and evidence IDs are in curation scope. "
        "Truncated or missing content was not reviewed."
    )


def build_weekly_maintenance_context(
    *,
    user_id: int,
    logical_date: str,
    period_key: str,
) -> WeeklyMaintenanceContext:
    monday = date.fromisoformat(logical_date)
    if monday.weekday() != 0:
        raise ValueError("weekly logical date must be Monday")
    window_end = datetime.combine(monday, time.min, tzinfo=config.TZ)
    window_start = window_end - timedelta(days=7)
    diary = read_diary_archive_window(window_start.date(), window_end.date())
    package = build_weekly_memory_candidate_package(
        user_id, window_start, window_end,
    )
    recent_messages = get_recent_user_messages_in_window(
        user_id, window_start, window_end,
    )
    allowed_evidence = frozenset(
        set(package.allowed_evidence_message_ids)
        | {message["id"] for message in recent_messages}
    )
    return WeeklyMaintenanceContext(
        logical_date=logical_date,
        period_key=period_key,
        window_start=window_start,
        window_end=window_end,
        diary=diary,
        package=package,
        recent_user_messages=tuple(recent_messages),
        allowed_item_ids=package.allowed_item_ids,
        allowed_evidence_message_ids=allowed_evidence,
        rendered=_render_context(
            logical_date, diary, package, recent_messages,
        ),
    )


@dataclass
class WeeklyMaintenanceSession:
    user_id: int
    context: WeeklyMaintenanceContext
    core_succeeded: bool = False
    curation_succeeded: bool = False

    def definitions(self) -> list[dict]:
        definitions = []
        if not self.core_succeeded:
            definitions.append(_CORE_DEFINITION)
        if (
            not self.curation_succeeded
            and (
                self.context.allowed_item_ids
                or self.context.allowed_evidence_message_ids
            )
        ):
            definitions.append(_CURATE_DEFINITION)
        return definitions

    def owns(self, tool_name: str) -> bool:
        return tool_name in {CORE_TOOL, CURATE_TOOL}

    async def execute(self, tool_name: str, args: dict) -> SkillResult:
        if tool_name == CORE_TOOL:
            return await self._update_core(args)
        if tool_name == CURATE_TOOL:
            return await self._curate(args)
        return SkillResult(output=f"Unknown Weekly tool: {tool_name}", success=False)

    async def complete_pending_projection(self) -> bool:
        """Finish a committed receipt without replaying Main or curation."""
        receipt = await asyncio.to_thread(
            get_weekly_curation_receipt,
            self.user_id,
            self.context.period_key,
        )
        if receipt is None:
            return False
        self.curation_succeeded = True
        if receipt.projection_status == "success":
            return True
        projection_ids = list(receipt.result.kg_reprojection_ids)
        if not projection_ids:
            await asyncio.to_thread(
                set_weekly_projection_state,
                self.user_id,
                self.context.period_key,
                status="success",
            )
            return True
        try:
            from mochi.knowledge_graph import project_memory_items
            await asyncio.to_thread(
                project_memory_items,
                self.user_id,
                projection_ids,
            )
        except Exception as exc:
            await asyncio.to_thread(
                set_weekly_projection_state,
                self.user_id,
                self.context.period_key,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise WeeklyProjectionError(
                "Weekly KG projection remains incomplete"
            ) from exc
        await asyncio.to_thread(
            set_weekly_projection_state,
            self.user_id,
            self.context.period_key,
            status="success",
        )
        return True

    async def _update_core(self, args: dict) -> SkillResult:
        if set(args) != {"expected_content", "operations"}:
            return SkillResult(
                output="Weekly Core update accepts an exact snapshot and patch operations.",
                success=False,
            )
        if self.core_succeeded:
            return SkillResult(
                output="Weekly Core update already completed.",
                success=False,
            )
        try:
            outcome = await asyncio.to_thread(
                update_weekly_core_exact,
                user_id=self.user_id,
                period_key=self.context.period_key,
                expected_content=args["expected_content"],
                operations=args["operations"],
            )
        except CoreError as exc:
            return SkillResult(
                output=f"Weekly Core update rejected: {exc}",
                success=False,
            )
        if outcome == "conflict":
            return SkillResult(
                output="Weekly Core update rejected: Core changed after packaging.",
                success=False,
            )
        self.core_succeeded = True
        if outcome == "replayed":
            return SkillResult(
                output="Weekly Core patch already committed for this week.",
                summary="Weekly Core patch replayed safely.",
            )
        return SkillResult(
            output="Weekly Core patch committed with a snapshot.",
            summary="Weekly Core patch committed.",
            state_changed=True,
        )

    async def _curate(self, args: dict) -> SkillResult:
        if set(args) != {"operations"}:
            return SkillResult(
                output="Weekly curation accepts only the operations array.",
                success=False,
            )
        if self.curation_succeeded:
            return SkillResult(
                output="Weekly curation already completed.",
                success=False,
            )
        try:
            result = await asyncio.to_thread(
                curate_memory_items,
                self.user_id,
                self.context.allowed_item_ids,
                self.context.allowed_evidence_message_ids,
                args["operations"],
                period_key=self.context.period_key,
            )
        except (MemoryCurationError, TypeError, KeyError) as exc:
            return SkillResult(
                output=f"Weekly curation rejected: {exc}",
                success=False,
            )
        self.curation_succeeded = True
        await self.complete_pending_projection()
        changed_ids = list(dict.fromkeys((
            *result.created_ids,
            *result.changed_ids,
            *result.archived_ids,
        )))
        receipt = (
            f"Weekly curation {'replayed safely' if result.replayed else 'committed'}: "
            f"created={list(result.created_ids)}, "
            f"changed={list(result.changed_ids)}, "
            f"archived={list(result.archived_ids)}."
        )
        return SkillResult(
            output=receipt,
            summary=receipt,
            entity_refs=[f"memory:{item_id}" for item_id in changed_ids],
            state_changed=bool(changed_ids) and not result.replayed,
        )


def create_weekly_session(
    *,
    user_id: int,
    logical_date: str,
    period_key: str,
) -> WeeklyMaintenanceSession:
    return WeeklyMaintenanceSession(
        user_id=user_id,
        context=build_weekly_maintenance_context(
            user_id=user_id,
            logical_date=logical_date,
            period_key=period_key,
        ),
        core_succeeded=has_weekly_core_update(user_id, period_key),
    )
