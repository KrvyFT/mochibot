from datetime import datetime, timedelta, timezone

import pytest

from mochi.ai_client import (
    _format_current_time_context,
    _tool_loop_exhaustion_message,
)


def test_current_time_context_includes_deterministic_weekday():
    now = datetime(
        2026, 8, 17, 10, 3, 41,
        tzinfo=timezone(timedelta(hours=8)),
    )

    assert _format_current_time_context(now) == (
        "当前时间：2026-08-17 10:03:41 +0800（星期一）"
    )


@pytest.mark.parametrize(
    ("successful_effects", "tool_audit", "expected"),
    [
        (
            True,
            [
                {"status": "failed", "state_changed": False},
                {"status": "success", "state_changed": True},
            ],
            "刚才只处理成功了一部分，剩下的还没改完。",
        ),
        (
            True,
            [{"status": "success", "state_changed": True}],
            "处理已经完成。",
        ),
        (
            False,
            [{"status": "failed", "state_changed": False}],
            "处理过程出了点问题，你再说一次试试？",
        ),
    ],
)
def test_tool_loop_exhaustion_reports_actual_outcome(
    successful_effects, tool_audit, expected,
):
    assert _tool_loop_exhaustion_message(
        successful_effects=successful_effects,
        tool_audit=tool_audit,
    ) == expected
