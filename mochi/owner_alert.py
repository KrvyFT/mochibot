"""Out-of-band failure notifications to the bot owner.

A failure inside a chat turn can answer in the reply itself. A failure in
scheduled work — free-time thoughts, bedtime entries, self-reminders, weekly
maintenance — has nowhere to go: the caller returns a silent disposition and
the only trace is a log line on a machine nobody is watching. This module
carries those failures to the owner's transport.

Alerts are deduplicated per reason. When connectivity is the problem every
scheduled run fails the same way, and the alert needs that same connectivity,
so repeating it would queue up the one message least likely to arrive.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

AlertSender = Callable[[str], Awaitable[bool]]

# An alert is a courtesy, not the work. Cap it so a wedged transport cannot
# hold up the caller that is already handling the original failure.
_SEND_TIMEOUT_S = 20.0

# Reasons come from a small fixed vocabulary (exception type names), so growth
# past this bound means a caller is generating them dynamically and the oldest
# entries are the safest to forget.
_MAX_TRACKED_REASONS = 64

_sender: AlertSender | None = None
_last_sent_at: dict[str, float] = {}


def set_alert_sender(sender: AlertSender | None) -> None:
    """Register the transport callback used to reach the owner."""
    global _sender
    _sender = sender


def reset() -> None:
    """Drop the registered sender and the cooldown window."""
    global _sender
    _sender = None
    _last_sent_at.clear()


def _claim(reason: str, cooldown_s: float) -> bool:
    """Reserve the right to alert about ``reason``, honouring its cooldown."""
    now = time.monotonic()
    previous = _last_sent_at.get(reason)
    if previous is not None and now - previous < cooldown_s:
        return False
    if reason not in _last_sent_at and len(_last_sent_at) >= _MAX_TRACKED_REASONS:
        oldest = min(_last_sent_at, key=_last_sent_at.__getitem__)
        del _last_sent_at[oldest]
    _last_sent_at[reason] = now
    return True


async def alert_owner(reason: str, text: str) -> bool:
    """Send ``text`` to the owner unless ``reason`` is still in cooldown.

    Returns whether the transport reported delivery. Never raises: an alert
    about connectivity is itself likely to fail, and it must not displace the
    failure the caller is already handling.

    The cooldown stamp is kept even when delivery fails. Clearing it would let
    every subsequent scheduled failure start another doomed send, and the
    cooldown only postpones the eventual successful alert.
    """
    from mochi.config import OWNER_ALERT_COOLDOWN_S, OWNER_ALERT_ENABLED

    if not OWNER_ALERT_ENABLED:
        return False
    sender = _sender
    if sender is None:
        log.debug("No alert sender registered; dropping alert %s", reason)
        return False
    if not _claim(reason, OWNER_ALERT_COOLDOWN_S):
        log.debug("Alert %s suppressed by cooldown", reason)
        return False
    try:
        delivered = await asyncio.wait_for(
            sender(text), timeout=_SEND_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Owner alert %s could not be delivered: %s", reason, exc)
        return False
    if not delivered:
        log.warning("Owner alert %s was not accepted by the transport", reason)
    return bool(delivered)
