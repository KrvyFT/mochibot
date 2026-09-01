"""Internal search skill — bounded local search across personal history."""

import logging
import re
import sqlite3

from mochi.db import recall_memory, search_conversation_messages
from mochi.diary import search_diary_entries
from mochi.skills.base import Skill, SkillContext, SkillResult

log = logging.getLogger(__name__)

_SOURCES = frozenset({"all", "conversation", "diary", "memory"})
_EXCERPT_CHARS = 280


def _excerpt(content: str, query: str) -> str:
    compact = re.sub(r"\s+", " ", content).strip()
    if len(compact) <= _EXCERPT_CHARS:
        return compact

    match_at = compact.casefold().find(query.casefold())
    start = max(0, match_at - _EXCERPT_CHARS // 3) if match_at >= 0 else 0
    end = min(len(compact), start + _EXCERPT_CHARS)
    if end == len(compact):
        start = max(0, end - _EXCERPT_CHARS)
    return (
        ("…" if start else "")
        + compact[start:end].strip()
        + ("…" if end < len(compact) else "")
    )


def _limit_arg(args: dict) -> int | SkillResult:
    value = args.get("limit", 5)
    if isinstance(value, bool):
        return SkillResult(output="limit must be an integer.", success=False)
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return SkillResult(output="limit must be an integer.", success=False)
    if not 1 <= limit <= 10:
        return SkillResult(
            output="limit must be between 1 and 10.",
            success=False,
        )
    return limit


class InternalSearchSkill(Skill):

    async def execute(self, context: SkillContext) -> SkillResult:
        if context.tool_name != "search_personal_history":
            return SkillResult(
                output=f"Unknown tool: {context.tool_name}",
                success=False,
            )

        query = context.args.get("query")
        if not isinstance(query, str) or not query.strip():
            return SkillResult(output="query is required.", success=False)
        query = query.strip()
        if len(query) > 200:
            return SkillResult(
                output="query must be 200 characters or fewer.",
                success=False,
            )

        source = context.args.get("source", "all")
        if not isinstance(source, str) or source not in _SOURCES:
            return SkillResult(
                output=(
                    "source must be all, conversation, diary, or memory."
                ),
                success=False,
            )
        limit = _limit_arg(context.args)
        if isinstance(limit, SkillResult):
            return limit

        try:
            sections, notices = self._search(
                user_id=context.user_id,
                turn_id=context.turn_id,
                query=query,
                source=source,
                limit=limit,
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            log.error("Internal search failed: %s", exc, exc_info=True)
            return SkillResult(
                output=f"Internal search failed: {exc}",
                success=False,
            )

        matched = sum(len(items) for _, items in sections)
        if not matched:
            notice = f" {' '.join(notices)}" if notices else ""
            return SkillResult(
                output=f'No local matches found for "{query}".{notice}',
            )

        lines = [f'Local matches for "{query}":']
        for label, items in sections:
            if not items:
                continue
            lines.extend(["", f"{label} ({len(items)}):", *items])
        if notices:
            lines.extend(["", *notices])
        return SkillResult(output="\n".join(lines))

    @staticmethod
    def _search(
        *,
        user_id: int,
        turn_id: str,
        query: str,
        source: str,
        limit: int,
    ) -> tuple[list[tuple[str, list[str]]], list[str]]:
        sections: list[tuple[str, list[str]]] = []
        notices: list[str] = []

        if source in {"all", "conversation"}:
            messages = search_conversation_messages(
                user_id,
                query,
                limit=limit,
                exclude_turn_id=turn_id or None,
            )
            sections.append((
                "Conversation",
                [
                    (
                        f"- {item['created_at']} [{item['role']}]: "
                        f"{_excerpt(item['content'], query)}"
                    )
                    for item in messages
                ],
            ))

        if source in {"all", "diary"}:
            diary_entries, diary_truncated = search_diary_entries(
                query,
                limit=limit,
            )
            sections.append((
                "Diary",
                [
                    f"- {item['date']}: {_excerpt(item['content'], query)}"
                    for item in diary_entries
                ],
            ))
            if diary_truncated:
                notices.append(
                    "Diary archive scan reached its local safety limit; "
                    "older files were not checked."
                )

        if source in {"all", "memory"}:
            memories = recall_memory(
                user_id,
                query=query,
                limit=limit,
                bump_access=False,
            )
            sections.append((
                "Memory",
                [
                    (
                        f"- #{item['id']} "
                        f"({item['evidence_start'] or item['updated_at'][:10]}): "
                        f"{_excerpt(item['content'], query)}"
                    )
                    for item in memories
                ],
            ))

        return sections, notices
