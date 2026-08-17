from datetime import datetime, timedelta, timezone

from mochi.ai_client import _format_current_time_context


def test_current_time_context_includes_deterministic_weekday():
    now = datetime(
        2026, 8, 17, 10, 3, 41,
        tzinfo=timezone(timedelta(hours=8)),
    )

    assert _format_current_time_context(now) == (
        "当前时间：2026-08-17 10:03:41 +0800（星期一）"
    )
