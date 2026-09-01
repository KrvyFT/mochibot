"""Trailing debounce for coalescing rapid owner messages into one turn.

Telegram uses this so several texts/photos become a single Main call after a
quiet window. The helper is transport-agnostic and does not import Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Generic, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")
OnFlush = Callable[[list[T]], Awaitable[None]]


def aggregate_user_turn_text(items: Sequence[tuple[str, bool]]) -> str:
    """Join buffered texts; earlier photos become ``[图片]`` placeholders.

    ``items`` is ``(text, is_image)``. The last image keeps its raw caption so
    the caller can attach those bytes on ``IncomingMessage.image``.
    """
    last_image_index = None
    for index, (_, is_image) in enumerate(items):
        if is_image:
            last_image_index = index
    parts: list[str] = []
    for index, (text, is_image) in enumerate(items):
        caption = (text or "").strip()
        if is_image and index != last_image_index:
            parts.append(f"[图片] {caption}".strip() if caption else "[图片]")
            continue
        if caption:
            parts.append(caption)
    return "\n\n".join(parts)


class MessageDebouncer(Generic[T]):
    """Per-chat trailing debounce with a hard buffer cap."""

    def __init__(
        self,
        *,
        delay_s: float,
        max_items: int = 20,
        max_chars: int = 8000,
        clock: Callable[[], float] | None = None,
        on_runner_start: Callable[[], None] | None = None,
    ) -> None:
        self.delay_s = delay_s
        self.max_items = max_items
        self.max_chars = max_chars
        self._clock = clock or time.monotonic
        self._on_runner_start = on_runner_start
        self._lock = asyncio.Lock()
        self._buffers: dict[int, list[T]] = {}
        self._char_counts: dict[int, int] = {}
        self._last_arrival: dict[int, float] = {}
        self._nudge: dict[int, asyncio.Event] = {}
        self._flush_tasks: dict[int, asyncio.Task[None]] = {}

    async def enqueue(
        self,
        key: int,
        item: T,
        *,
        text: str,
        on_flush: OnFlush[T],
    ) -> asyncio.Task[None]:
        """Buffer ``item`` and ensure a runner is waiting for a quiet window."""
        async with self._lock:
            buf = self._buffers.setdefault(key, [])
            buf.append(item)
            self._char_counts[key] = self._char_counts.get(key, 0) + len(text or "")
            self._last_arrival[key] = self._clock()
            over_cap = (
                len(buf) >= self.max_items
                or self._char_counts[key] >= self.max_chars
            )
            if over_cap:
                # Quiet window already elapsed: flush as soon as the waiter wakes.
                self._last_arrival[key] = self._clock() - max(self.delay_s, 0.0)
                event = self._nudge.get(key)
                if event is not None and not event.is_set():
                    event.set()
            task = self._flush_tasks.get(key)
            if task is None or task.done():
                task = asyncio.create_task(
                    self._run(key, on_flush),
                    name=f"message-debounce:{key}",
                )
                self._flush_tasks[key] = task
            return task

    async def cancel_all(self) -> None:
        """Drop pending buffers and cancel waiters (used on transport stop)."""
        while True:
            async with self._lock:
                tasks = list(self._flush_tasks.values())
                self._buffers.clear()
                self._char_counts.clear()
                self._last_arrival.clear()
                for event in self._nudge.values():
                    event.set()
                self._flush_tasks.clear()
            if not tasks:
                async with self._lock:
                    self._nudge.clear()
                return
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, key: int, on_flush: OnFlush[T]) -> None:
        if self._on_runner_start is not None:
            self._on_runner_start()
        try:
            while True:
                items = await self._wait_quiet_and_take(key)
                if not items:
                    return
                try:
                    await on_flush(items)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Debounced flush failed for chat %s", key)
        finally:
            async with self._lock:
                current = asyncio.current_task()
                if self._flush_tasks.get(key) is not current:
                    return
                if self._buffers.get(key):
                    self._flush_tasks[key] = asyncio.create_task(
                        self._run(key, on_flush),
                        name=f"message-debounce:{key}",
                    )
                    return
                self._flush_tasks.pop(key, None)

    async def _wait_quiet_and_take(self, key: int) -> list[T]:
        while True:
            async with self._lock:
                if not self._buffers.get(key):
                    return []
                remaining = (
                    self._last_arrival[key] + max(self.delay_s, 0.0) - self._clock()
                )
                if remaining <= 0:
                    return self._take_locked(key)
                event = self._nudge.setdefault(key, asyncio.Event())
                event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except TimeoutError:
                pass

    def _take_locked(self, key: int) -> list[T]:
        items = self._buffers.pop(key, [])
        self._char_counts.pop(key, None)
        self._last_arrival.pop(key, None)
        return items
