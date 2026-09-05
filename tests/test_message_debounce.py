"""Trailing debounce for coalescing owner messages."""

import asyncio

import pytest

from mochi.transport.message_debounce import (
    MessageDebouncer,
    aggregate_user_turn_text,
)


def test_aggregate_joins_texts_with_blank_lines():
    assert aggregate_user_turn_text(
        [("晚上吃啥", False), ("别太油", False)],
    ) == "晚上吃啥\n\n别太油"


def test_aggregate_marks_earlier_images_and_keeps_last_caption():
    assert aggregate_user_turn_text(
        [("菜单", True), ("这个呢", True), ("少油", False)],
    ) == "[图片] 菜单\n\n这个呢\n\n少油"


def test_aggregate_last_image_keeps_raw_caption():
    assert aggregate_user_turn_text(
        [("先看这个", False), ("请看看这张图片。", True)],
    ) == "先看这个\n\n请看看这张图片。"


@pytest.mark.asyncio
async def test_single_message_flushes_after_quiet_window():
    flushed: list[list[str]] = []
    debouncer = MessageDebouncer(delay_s=0.05)

    async def on_flush(items: list[str]) -> None:
        flushed.append(list(items))

    await debouncer.enqueue(1, "a", text="a", on_flush=on_flush)
    await asyncio.sleep(0.02)
    assert flushed == []
    await asyncio.sleep(0.06)
    assert flushed == [["a"]]


@pytest.mark.asyncio
async def test_later_message_resets_quiet_window():
    flushed: list[list[str]] = []
    debouncer = MessageDebouncer(delay_s=0.08)

    async def on_flush(items: list[str]) -> None:
        flushed.append(list(items))

    await debouncer.enqueue(1, "a", text="a", on_flush=on_flush)
    await asyncio.sleep(0.05)
    await debouncer.enqueue(1, "b", text="b", on_flush=on_flush)
    await asyncio.sleep(0.05)
    assert flushed == []
    await asyncio.sleep(0.06)
    assert flushed == [["a", "b"]]


@pytest.mark.asyncio
async def test_messages_during_flush_wait_another_quiet_window():
    """After on_flush returns, mid-flush arrivals still need a full quiet window."""
    flushed: list[list[str]] = []
    started = asyncio.Event()
    release = asyncio.Event()
    debouncer = MessageDebouncer(delay_s=0.08)

    async def on_flush(items: list[str]) -> None:
        flushed.append(list(items))
        started.set()
        await release.wait()

    await debouncer.enqueue(1, "a", text="a", on_flush=on_flush)
    await asyncio.wait_for(started.wait(), timeout=1)
    assert flushed == [["a"]]
    await debouncer.enqueue(1, "b", text="b", on_flush=on_flush)
    await debouncer.enqueue(1, "c", text="c", on_flush=on_flush)
    release.set()
    await asyncio.sleep(0.04)
    assert flushed == [["a"]]
    await asyncio.sleep(0.08)
    assert flushed == [["a"], ["b", "c"]]


@pytest.mark.asyncio
async def test_telegram_flush_does_not_drain_mid_reply_backlog(monkeypatch):
    """Replies in flight must not steal buffered messages and skip debounce."""
    import mochi.transport.telegram as tg

    monkeypatch.setattr(tg, "TG_AGGREGATE_ENABLED", True)
    monkeypatch.setattr(tg, "TG_MESSAGE_DEBOUNCE_S", 0.08)
    monkeypatch.setattr(
        tg.TelegramTransport, "_dispatch_state_signals", staticmethod(lambda: None),
    )

    started = asyncio.Event()
    release = asyncio.Event()
    runs: list[str] = []

    async def slow_grouped(self, pending):
        runs.append(pending.text)
        started.set()
        await release.wait()

    monkeypatch.setattr(
        tg.TelegramTransport, "_run_topic_grouped_turns", slow_grouped,
    )

    async def unused_callback(_msg):
        raise AssertionError("Main must not run before debounce flush")

    tg.set_message_handler(unused_callback)
    transport = tg.TelegramTransport()
    try:
        class _Message:
            photo = None
            caption = None

            def __init__(self, text: str, message_id: int):
                self.text = text
                self.message_id = message_id

            async def reply_text(self, *_args, **_kwargs):
                return None

        class _Update:
            def __init__(self, text: str, message_id: int):
                self.message = _Message(text, message_id)
                self.effective_user = type("U", (), {"id": 1})()
                self.effective_chat = type("C", (), {"id": 42})()

        await transport._handle_message(_Update("first", 1), None)
        await asyncio.wait_for(started.wait(), timeout=1)
        assert runs == ["first"]

        await transport._handle_message(_Update("second", 2), None)
        release.set()
        await asyncio.sleep(0.04)
        assert runs == ["first"]
        await asyncio.sleep(0.1)
        assert runs == ["first", "second"]
    finally:
        tg.set_message_handler(None)
        await transport.stop()


@pytest.mark.asyncio
async def test_over_cap_flushes_without_waiting_full_delay():
    flushed: list[list[str]] = []
    debouncer = MessageDebouncer(delay_s=2.0, max_items=2, max_chars=8000)

    async def on_flush(items: list[str]) -> None:
        flushed.append(list(items))

    await debouncer.enqueue(1, "a", text="a", on_flush=on_flush)
    await debouncer.enqueue(1, "b", text="b", on_flush=on_flush)
    await asyncio.sleep(0.05)
    assert flushed == [["a", "b"]]


@pytest.mark.asyncio
async def test_cancel_all_drops_pending_without_flush():
    flushed: list[list[str]] = []
    debouncer = MessageDebouncer(delay_s=1.0)

    async def on_flush(items: list[str]) -> None:
        flushed.append(list(items))

    await debouncer.enqueue(1, "a", text="a", on_flush=on_flush)
    await debouncer.cancel_all()
    await asyncio.sleep(0.05)
    assert flushed == []


@pytest.mark.asyncio
async def test_drain_takes_items_buffered_during_flush():
    debouncer = MessageDebouncer(delay_s=0.05)
    started = asyncio.Event()
    release = asyncio.Event()

    async def on_flush(items: list[str]) -> None:
        started.set()
        await release.wait()

    await debouncer.enqueue(1, "a", text="a", on_flush=on_flush)
    await asyncio.wait_for(started.wait(), timeout=1)
    await debouncer.enqueue(1, "b", text="b", on_flush=on_flush)
    drained = await debouncer.drain(1)
    assert drained == ["b"]
    release.set()
    await asyncio.sleep(0.02)

@pytest.mark.asyncio
async def test_telegram_defers_main_until_quiet_window(monkeypatch):
    import mochi.transport.telegram as tg
    from mochi.ai_client import ChatResult

    monkeypatch.setattr(tg, "TG_AGGREGATE_ENABLED", True)
    monkeypatch.setattr(tg, "TG_MESSAGE_DEBOUNCE_S", 0.05)
    monkeypatch.setattr(tg.TelegramTransport, "_dispatch_state_signals", staticmethod(lambda: None))

    seen: list[str] = []

    async def callback(msg):
        seen.append(msg.text)
        return ChatResult(text="")

    tg.set_message_handler(callback)
    transport = tg.TelegramTransport()
    try:
        class _Message:
            text = "hello"
            photo = None
            caption = None
            message_id = 9

            async def reply_text(self, *_args, **_kwargs):
                return None

        class _Update:
            message = _Message()
            effective_user = type("U", (), {"id": 1})()
            effective_chat = type("C", (), {"id": 42})()

        await transport._handle_message(_Update(), None)
        await asyncio.sleep(0.02)
        assert seen == []
        await asyncio.sleep(0.08)
        assert seen == ["hello"]
    finally:
        tg.set_message_handler(None)
        await transport.stop()
