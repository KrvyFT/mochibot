import pytest

import mochi.conversation_summary as summary_worker
from mochi.db import get_conversation_summary_status, save_message
from mochi.llm import LLMResponse


class Client:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        if isinstance(output, tuple):
            content, finish_reason = output
        else:
            content, finish_reason = output, "stop"
        return LLMResponse(
            content=content,
            model="lite-test",
            finish_reason=finish_reason,
        )


def _save_turns(count):
    for number in range(count):
        turn_id = f"turn-{number}"
        save_message(1, "user", f"user-{number}", turn_id=turn_id)
        save_message(1, "assistant", f"assistant-{number}", turn_id=turn_id)


@pytest.fixture(autouse=True)
def summary_state(monkeypatch):
    monkeypatch.setattr(summary_worker, "SUMMARY_BATCH_SIZE", 2)
    summary_worker._tasks.clear()


@pytest.mark.asyncio
async def test_provider_failure_retries_same_batch_before_advancing(monkeypatch):
    _save_turns(2)
    failed = Client([RuntimeError("offline")])
    monkeypatch.setattr(
        summary_worker, "get_client_for_tier", lambda _tier: failed,
    )
    await summary_worker.schedule_conversation_summary(1)
    assert get_conversation_summary_status(1, 2)["pending_turns"] == 2

    recovered = Client(["recovered"])
    monkeypatch.setattr(
        summary_worker, "get_client_for_tier", lambda _tier: recovered,
    )
    await summary_worker.schedule_conversation_summary(1)
    assert recovered.calls[0]["messages"][1]["content"] == (
        failed.calls[0]["messages"][1]["content"]
    )
    status = get_conversation_summary_status(1, 2)
    assert status["summary"] == "recovered"
    assert status["pending_turns"] == 0


@pytest.mark.asyncio
async def test_length_truncation_retries_from_full_input_before_advancing(
    monkeypatch,
):
    _save_turns(2)
    client = Client([
        ("half a summary ending in egg", "length"),
        ("complete compact summary.", "stop"),
    ])
    monkeypatch.setattr(
        summary_worker, "get_client_for_tier", lambda _tier: client,
    )

    await summary_worker.schedule_conversation_summary(1)

    assert len(client.calls) == 2
    assert client.calls[0]["max_tokens"] >= 1200
    assert (
        client.calls[1]["messages"][1]["content"]
        == client.calls[0]["messages"][1]["content"]
    )
    assert "half a summary" not in str(client.calls[1]["messages"])
    status = get_conversation_summary_status(1, 2)
    assert status["summary"] == "complete compact summary."
    assert status["pending_turns"] == 0


@pytest.mark.asyncio
async def test_second_length_truncation_keeps_summary_and_cursor(monkeypatch):
    _save_turns(2)
    client = Client([
        ("first incomplete", "length"),
        ("second incomplete", "max_tokens"),
    ])
    monkeypatch.setattr(
        summary_worker, "get_client_for_tier", lambda _tier: client,
    )

    await summary_worker.schedule_conversation_summary(1)

    status = get_conversation_summary_status(1, 2)
    assert status["summary"] == ""
    assert status["pending_turns"] == 2
    assert "truncated" in status["last_error"]


@pytest.mark.asyncio
async def test_oversized_batch_shrinks_without_dropping_turns(monkeypatch):
    _save_turns(5)
    client = Client(["first pair.", "second pair.", "tail turn."])
    monkeypatch.setattr(summary_worker, "SUMMARY_BATCH_SIZE", 4)
    monkeypatch.setattr(
        summary_worker, "get_client_for_tier", lambda _tier: client,
    )
    monkeypatch.setattr(
        summary_worker,
        "_fits_context",
        lambda _prompt, summary_input, _output_tokens: (
            summary_input.count(" user: ") <= 2
        ),
    )

    await summary_worker.schedule_conversation_summary(1)

    assert len(client.calls) == 3
    combined_inputs = "\n".join(
        call["messages"][1]["content"] for call in client.calls
    )
    for number in range(5):
        assert f"user-{number}" in combined_inputs
    status = get_conversation_summary_status(1, 4)
    assert status["summary"] == "tail turn."
    assert status["pending_turns"] == 0
    assert status["last_error"] is None
