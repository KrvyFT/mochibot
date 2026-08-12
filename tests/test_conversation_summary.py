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
        return LLMResponse(content=output, model="lite-test")


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
async def test_summary_advances_in_complete_batches(monkeypatch):
    client = Client(["summary"])
    monkeypatch.setattr(
        summary_worker, "get_client_for_tier", lambda _tier: client,
    )
    _save_turns(2)

    await summary_worker.schedule_conversation_summary(1)

    status = get_conversation_summary_status(1, 2)
    assert status["summary"] == "summary"
    assert status["pending_turns"] == 0


@pytest.mark.asyncio
async def test_summary_failure_retries_same_batch(monkeypatch):
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
