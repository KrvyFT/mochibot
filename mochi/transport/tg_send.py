"""Reliable Telegram Bot API send with timeout + infinite retry."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from mochi.config import (
    TG_SEND_MAX_ATTEMPTS,
    TG_SEND_MAX_BACKOFF_S,
    TG_SEND_QUICK_RETRIES,
    TG_SEND_TIMEOUT_S,
)

log = logging.getLogger(__name__)


async def call_telegram_api(
    factory: Callable[[], Awaitable[object]],
    *,
    label: str,
    timeout_s: float | None = None,
    quick_retries: int | None = None,
    max_attempts: int | None = None,
    max_backoff_s: float | None = None,
) -> bool:
    """Run ``factory`` until success.

    First ``quick_retries`` attempts use short backoff (1s, 2s, 4s…).
    Afterwards backoff grows up to ``max_backoff_s`` and retries forever
    unless ``max_attempts`` is a positive cap (tests only).
    """
    timeout = TG_SEND_TIMEOUT_S if timeout_s is None else timeout_s
    quick = TG_SEND_QUICK_RETRIES if quick_retries is None else quick_retries
    cap = TG_SEND_MAX_ATTEMPTS if max_attempts is None else max_attempts
    max_backoff = TG_SEND_MAX_BACKOFF_S if max_backoff_s is None else max_backoff_s
    attempt = 0
    while True:
        attempt += 1
        try:
            await asyncio.wait_for(factory(), timeout=timeout)
            return True
        except Exception as exc:
            if cap > 0 and attempt >= cap:
                log.error(
                    "Telegram %s failed after %d attempts: %s",
                    label, attempt, exc,
                )
                return False
            if attempt <= quick:
                delay = float(2 ** (attempt - 1))
            else:
                delay = min(float(max_backoff), float(2 ** min(attempt - 1, 6)))
            log.warning(
                "Telegram %s failed (attempt %d): %s; retry in %.0fs",
                label, attempt, exc, delay,
            )
            await asyncio.sleep(delay)
