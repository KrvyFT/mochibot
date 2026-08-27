"""Activity Pattern Observer — conversation activity facts from SQLite.

Zero LLM calls. Reads per-day message counts without assigning meaning to them.
Main can inspect the trend through look_around and decide what, if anything, it
means in the current relationship context.
"""

from mochi.observers.base import DETAIL_ITEM_LIMIT, Observer


class ActivityPatternObserver(Observer):
    """Reports conversation activity over the last 7 days. No external API."""

    _SUMMARY_FIELDS = (
        "today_messages",
        "yesterday_messages",
        "daily_avg_7d",
        "change_from_yesterday",
        "consecutive_silent_days",
        "today_to_7d_average",
    )

    def overview_view(self, data: dict) -> dict:
        return self.select_view(
            data,
            scalar_fields=self._SUMMARY_FIELDS,
        )

    def detail_view(self, data: dict) -> dict:
        return self.select_view(
            data,
            scalar_fields=self._SUMMARY_FIELDS,
            list_fields={"weekly_trend": ("date", "count")},
            item_limit=DETAIL_ITEM_LIMIT,
        )

    def has_delta(self, prev: dict, curr: dict) -> bool:
        """Conversation volume alone does not create a Free Time card."""
        return False

    async def observe(self) -> dict:
        from mochi.config import OWNER_USER_ID
        from mochi.db import get_daily_message_counts

        user_id = OWNER_USER_ID
        if user_id is None:
            return {}

        # Get last 7 days (includes today, always 7 entries)
        daily = get_daily_message_counts(user_id, days=7)
        if not daily:
            return {}

        today_entry = daily[-1]
        yesterday_entry = daily[-2] if len(daily) >= 2 else None

        today_count = today_entry["count"]
        yesterday_count = yesterday_entry["count"] if yesterday_entry else 0

        # Past 7 days (excluding today for baseline)
        past_counts = [d["count"] for d in daily[:-1]]
        daily_avg_7d = (
            round(sum(past_counts) / len(past_counts), 1)
            if past_counts else 0.0
        )
        consecutive_silent_days = 0
        for item in reversed(daily):
            if item["count"] != 0:
                break
            consecutive_silent_days += 1

        result = {
            "today_messages": today_count,
            "yesterday_messages": yesterday_count,
            "daily_avg_7d": daily_avg_7d,
            "change_from_yesterday": today_count - yesterday_count,
            "consecutive_silent_days": consecutive_silent_days,
        }
        if daily_avg_7d > 0:
            result["today_to_7d_average"] = round(
                today_count / daily_avg_7d, 2,
            )

        # Include the 7-day trend (useful for LLM)
        result["weekly_trend"] = [
            {"date": d["date"], "count": d["count"]} for d in daily
        ]

        return result
