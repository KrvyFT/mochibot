"""Reminder Observer — surfaces upcoming reminders for heartbeat awareness."""

import logging

from mochi.observers.base import (
    DETAIL_ITEM_LIMIT,
    OVERVIEW_ITEM_LIMIT,
    ObservedFact,
    Observer,
)

log = logging.getLogger(__name__)


class ReminderObserver(Observer):
    """Exposes unfired reminders due within the next 2 hours."""

    def attention_facts(self, data: dict) -> list[ObservedFact]:
        return [
            ObservedFact(
                stable_key=f"upcoming:{item['id']}",
                facts={
                    "message": item.get("message", ""),
                    "remind_at": item.get("remind_at", ""),
                },
                freshness_seconds=2 * 3600,
            )
            for item in data.get("upcoming", [])[:6]
            if item.get("id") and item.get("remind_at")
        ]

    def _view(self, data: dict, *, limit: int) -> dict:
        facts = self.select_view(
            data,
            list_fields={"upcoming": ("message", "remind_at")},
            item_limit=limit,
        )
        upcoming = data.get("upcoming")
        if isinstance(upcoming, list):
            facts["upcoming_count"] = len(upcoming)
        return facts

    def overview_view(self, data: dict) -> dict:
        return self._view(data, limit=OVERVIEW_ITEM_LIMIT)

    def detail_view(self, data: dict) -> dict:
        return self._view(data, limit=DETAIL_ITEM_LIMIT)

    async def observe(self) -> dict:
        from mochi.config import OWNER_USER_ID
        from mochi.skills.reminder.queries import get_upcoming_reminders

        user_id = OWNER_USER_ID
        if user_id is None:
            return {}

        upcoming = get_upcoming_reminders(user_id, hours_ahead=2)
        if not upcoming:
            return {}

        return {
            "upcoming": [
                {
                    "id": r["id"],
                    "message": r["message"],
                    "remind_at": r["remind_at"],
                }
                for r in upcoming
            ],
        }
