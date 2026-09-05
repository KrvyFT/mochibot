"""Split coalesced owner messages into topic groups for per-group replies."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")
_PARSE_FAIL = object()


@dataclass(frozen=True)
class TopicGroup:
    """One topic slice of the owner's buffered messages."""

    texts: tuple[str, ...]
    user_msg_ids: tuple[int, ...]

    @property
    def combined_text(self) -> str:
        return "\n\n".join(part for part in self.texts if part.strip())

    @property
    def anchor_msg_id(self) -> int | None:
        """Legacy helper: last message id. Prefer ``choose_reply_anchor``."""
        return self.user_msg_ids[-1] if self.user_msg_ids else None


def _fallback_groups(
    items: list[tuple[str, int]],
) -> list[TopicGroup]:
    """One message per group when the model is unavailable."""
    groups: list[TopicGroup] = []
    for text, msg_id in items:
        groups.append(TopicGroup(texts=(text,), user_msg_ids=(msg_id,)))
    return groups or [TopicGroup(texts=("",), user_msg_ids=())]


def _parse_group_indices(raw: str, n: int) -> list[list[int]] | None:
    match = _JSON_BLOCK.search(raw or "")
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, list) or not groups:
        return None
    seen: set[int] = set()
    parsed: list[list[int]] = []
    for group in groups:
        if not isinstance(group, list) or not group:
            return None
        indices: list[int] = []
        for value in group:
            try:
                index = int(value)
            except (TypeError, ValueError):
                return None
            if index < 0 or index >= n or index in seen:
                return None
            seen.add(index)
            indices.append(index)
        parsed.append(indices)
    if seen != set(range(n)):
        return None
    return parsed


def _parse_reply_to(raw: str, n: int):
    """Return index, None for explicit null, or ``_PARSE_FAIL`` on bad JSON."""
    match = _JSON_BLOCK.search(raw or "")
    if not match:
        return _PARSE_FAIL
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return _PARSE_FAIL
    if not isinstance(payload, dict) or "reply_to" not in payload:
        return _PARSE_FAIL
    value = payload["reply_to"]
    if value is None:
        return None
    try:
        index = int(value)
    except (TypeError, ValueError):
        return _PARSE_FAIL
    if index < 0 or index >= n:
        return _PARSE_FAIL
    return index


async def split_user_topics(
    items: list[tuple[str, int]],
) -> list[TopicGroup]:
    """Partition ``(text, telegram_message_id)`` into ordered topic groups.

    Uses the lite model when available; falls back to one group per message.
    A single item always returns one group.
    """
    cleaned = [(text or "", int(msg_id)) for text, msg_id in items]
    if not cleaned:
        return []
    if len(cleaned) == 1:
        text, msg_id = cleaned[0]
        return [TopicGroup(texts=(text,), user_msg_ids=(msg_id,))]

    numbered = "\n".join(
        f"[{index}] {text.strip() or '(空)'}"
        for index, (text, _) in enumerate(cleaned)
    )
    prompt = (
        "把用户连续发送的几条消息按话题分组。同一话题的消息放同一组，"
        "保持原有时间顺序。只输出 JSON："
        '{"groups":[[0,1],[2],...]}，数组元素是消息下标，每个下标恰好出现一次。\n\n'
        f"消息：\n{numbered}"
    )
    try:
        from mochi.llm import get_client_for_tier

        client = get_client_for_tier("lite")
        response = await asyncio.to_thread(
            client.chat,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
        )
        content = (getattr(response, "content", None) or "").strip()
        parsed = _parse_group_indices(content, len(cleaned))
        if parsed is None:
            log.warning("Topic split JSON invalid; falling back per-message")
            return _fallback_groups(cleaned)
        return [
            TopicGroup(
                texts=tuple(cleaned[i][0] for i in indices),
                user_msg_ids=tuple(cleaned[i][1] for i in indices),
            )
            for indices in parsed
        ]
    except Exception:
        log.warning("Topic split failed; falling back per-message", exc_info=True)
        return _fallback_groups(cleaned)


async def choose_reply_anchor(
    group: TopicGroup,
    reply_text: str,
) -> int | None:
    """Pick which user message to Telegram-reply, or None for a plain send.

    Mimics everyday IM: most linear follow-ups do not use reply; reply only
    when pinning a specific earlier bubble. Failures default to no reply.
    """
    if not group.user_msg_ids:
        return None
    # Single-bubble turns have nothing to disambiguate — skip the lite call.
    if len(group.user_msg_ids) <= 1:
        return None

    numbered = "\n".join(
        f"[{index}] {text.strip() or '(空)'}"
        for index, text in enumerate(group.texts)
    )
    preview = (reply_text or "").strip()
    if len(preview) > 240:
        preview = preview[:240] + "…"
    prompt = (
        "你在模拟 Telegram / 微信里真人怎么用「回复某条消息」。\n"
        "用户发了一组消息，助手要回一段话。\n"
        "默认像日常跟聊：连贯接话、随口回应、顺着聊 → reply_to 必须为 null"
        "（不要回复）。\n"
        "只有在需要点名某一句、澄清、回跳更早一句、或中间夹了别的话时，"
        "才把 reply_to 设为那条的下标。\n"
        "只输出 JSON，不要解释：{\"reply_to\": null} 或 {\"reply_to\": 0}\n\n"
        f"用户消息：\n{numbered}\n\n"
        f"助手将要发送的开头：\n{preview or '(空)'}"
    )
    try:
        from mochi.llm import get_client_for_tier

        client = get_client_for_tier("lite")
        response = await asyncio.to_thread(
            client.chat,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
        )
        content = (getattr(response, "content", None) or "").strip()
        parsed = _parse_reply_to(content, len(group.user_msg_ids))
        if parsed is _PARSE_FAIL:
            log.debug("Reply-anchor JSON invalid; sending without reply")
            return None
        if parsed is None:
            return None
        return group.user_msg_ids[int(parsed)]
    except Exception:
        log.debug("Reply-anchor choice failed; sending without reply", exc_info=True)
        return None
