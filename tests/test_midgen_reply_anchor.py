"""Mid-generation backlog forces Telegram reply_to on the next first bubble."""

from types import SimpleNamespace

from mochi.transport.telegram import (
    _PendingTurn,
    _first_midgen_reply_target,
    _merge_pending_turns,
)


def _pending(
    text: str,
    msg_id: int,
    *,
    busy: bool = False,
    channel_id: int = 1,
) -> _PendingTurn:
    return _PendingTurn(
        user_id=1,
        channel_id=channel_id,
        text=text,
        image=None,
        update=SimpleNamespace(),
        context=SimpleNamespace(),
        user_msg_id=msg_id,
        source_items=[(text, msg_id)],
        arrived_during_busy=busy,
    )


def test_first_midgen_reply_target_skips_non_busy():
    items = [
        _pending("先到的安静消息", 10, busy=False),
        _pending("生成中第一条", 11, busy=True),
        _pending("生成中第二条", 12, busy=True),
    ]
    assert _first_midgen_reply_target(items) == 11


def test_merge_forces_reply_to_first_midgen_message():
    merged = _merge_pending_turns(
        [
            _pending("当然留下了", 101, busy=True),
            _pending("我猜你在想每天早上吃什么", 102, busy=True),
        ]
    )
    assert merged.arrived_during_busy is True
    assert merged.force_reply_to_msg_id == 101
    assert "当然留下了" in merged.text
    assert "我猜你在想" in merged.text


def test_merge_without_midgen_does_not_force_reply():
    merged = _merge_pending_turns(
        [
            _pending("安静窗合批", 201, busy=False),
            _pending("第二条", 202, busy=False),
        ]
    )
    assert merged.arrived_during_busy is False
    assert merged.force_reply_to_msg_id is None
