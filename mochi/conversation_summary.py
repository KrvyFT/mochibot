"""Non-blocking continuous summaries over complete ordinary chat turns."""

from __future__ import annotations

import asyncio
import logging

from mochi.config import CONV_SUMMARY_BATCH_TURNS
from mochi.db import (
    get_conversation_summary_batch,
    get_conversation_summary_status,
    list_conversation_summary_users,
    log_usage,
    record_conversation_summary_error,
    save_conversation_summary,
)
from mochi.llm import get_client_for_tier
from mochi.prompt_loader import get_prompt
from mochi.token_estimator import truncate_to_token_budget


log = logging.getLogger(__name__)

SUMMARY_BATCH_SIZE = CONV_SUMMARY_BATCH_TURNS
_tasks: dict[int, asyncio.Task] = {}


def _summary_input(claim: dict) -> str:
    previous = claim["summary"].strip() or "(empty)"
    lines = [
        "<previous_summary>",
        previous,
        "</previous_summary>",
        "",
        "<new_turns>",
    ]
    for turn in claim["turns"]:
        user = turn["user"]
        assistant = turn["assistant"]
        lines.extend([
            f"[{user['created_at']}] user: {user['content']}",
            f"[{assistant['created_at']}] assistant: {assistant['content']}",
        ])
    lines.append("</new_turns>")
    return "\n".join(lines)


def _normalize_summary(value: str, max_tokens: int) -> str:
    compact = " ".join((value or "").split())
    if not compact:
        return ""
    compact = compact[:max(600, max_tokens * 6)].rstrip()
    return truncate_to_token_budget(
        compact, max_tokens, suffix="",
    ).rstrip()


async def _generate_summary(claim: dict) -> str:
    from mochi.config import CONV_SUMMARY_MAX_TOKENS

    prompt = get_prompt("conv_summary")
    if not prompt:
        raise RuntimeError("Conversation summary prompt is missing")
    client = get_client_for_tier("lite")
    response = await asyncio.to_thread(
        client.chat,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": _summary_input(claim)},
        ],
        tools=None,
        max_tokens=CONV_SUMMARY_MAX_TOKENS,
        temperature=0.2,
    )
    if response.total_tokens:
        log_usage(
            response.prompt_tokens,
            response.completion_tokens,
            response.total_tokens,
            model=response.model,
            purpose="conversation_summary",
            model_role="LITE",
            call_type="background",
            usage_stage="rolling_update",
            reasoning_tokens=response.reasoning_tokens,
            cached_prompt_tokens=response.cached_prompt_tokens,
        )
    return _normalize_summary(
        response.content or "", CONV_SUMMARY_MAX_TOKENS,
    )


async def _drain_user(user_id: int) -> None:
    failed = False
    try:
        while True:
            claim = await asyncio.to_thread(
                get_conversation_summary_batch,
                user_id,
                SUMMARY_BATCH_SIZE,
            )
            if claim is None:
                return
            try:
                summary = await _generate_summary(claim)
                if not summary:
                    raise ValueError(
                        "Lite returned an empty conversation summary"
                    )
            except Exception as exc:
                failed = True
                await asyncio.to_thread(
                    record_conversation_summary_error,
                    claim,
                    f"{type(exc).__name__}: {exc}",
                )
                log.warning(
                    "Conversation summary failed for user %d: %s",
                    user_id,
                    exc,
                )
                return

            saved = await asyncio.to_thread(
                save_conversation_summary, claim, summary,
            )
            if saved:
                log.info(
                    "Conversation summary advanced for user %d through message %d",
                    user_id,
                    claim["next_through_message_id"],
                )
    finally:
        current = asyncio.current_task()
        if _tasks.get(user_id) is current:
            del _tasks[user_id]
        if not failed:
            status = await asyncio.to_thread(
                get_conversation_summary_status,
                user_id,
                SUMMARY_BATCH_SIZE,
            )
            if status["pending_turns"] >= SUMMARY_BATCH_SIZE:
                asyncio.get_running_loop().call_soon(
                    schedule_conversation_summary, user_id,
                )


def schedule_conversation_summary(user_id: int) -> asyncio.Task | None:
    """Start one non-blocking worker per user; duplicate wakeups coalesce."""
    existing = _tasks.get(user_id)
    if existing is not None and not existing.done():
        return existing
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug("Conversation summary scheduling skipped without an event loop")
        return None
    task = loop.create_task(
        _drain_user(user_id),
        name=f"conversation-summary-{user_id}",
    )
    _tasks[user_id] = task
    return task


def resume_conversation_summaries() -> None:
    """Recover per-user backlog from durable state after startup."""
    for user_id in list_conversation_summary_users():
        schedule_conversation_summary(user_id)
