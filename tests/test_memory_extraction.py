import json

import pytest

import mochi.memory_extraction as extraction
from mochi.db import _connect, get_memory_extraction_status, save_message
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
    rows = []
    for number in range(count):
        turn_id = f"turn-{number}"
        user_id = save_message(1, "user", f"user-{number}", turn_id=turn_id)
        assistant_id = save_message(
            1, "assistant", f"assistant-{number}", turn_id=turn_id,
        )
        rows.append((user_id, assistant_id))
    return rows


@pytest.fixture(autouse=True)
def extraction_state(monkeypatch):
    monkeypatch.setattr(extraction, "EXTRACTION_BATCH_SIZE", 2)
    monkeypatch.setattr(extraction, "get_pool", lambda: type(
        "Pool", (), {"embed_batch": lambda self, texts: [None] * len(texts)},
    )())


def test_extraction_retries_same_batch_then_creates_evidence_memory(monkeypatch):
    turns = _save_turns(2)
    failed = Client([RuntimeError("offline")])
    monkeypatch.setattr(extraction, "get_client_for_tier", lambda _tier: failed)
    assert extraction.drain_memory_extraction(1) == 0
    assert get_memory_extraction_status(1, 2)["pending_turns"] == 2

    candidate = json.dumps([{
        "content": "\u559c\u6b22\u5468\u672b\u722c\u5c71",
        "importance": 2,
        "evidence_message_ids": [turns[0][0]],
    }], ensure_ascii=False)
    recovered = Client([candidate])
    monkeypatch.setattr(extraction, "get_client_for_tier", lambda _tier: recovered)
    assert extraction.drain_memory_extraction(1) == 1
    assert recovered.calls[0]["messages"][1]["content"] == (
        failed.calls[0]["messages"][1]["content"]
    )
    row = _connect().execute(
        "SELECT content, evidence_message_ids FROM memory_items"
    ).fetchone()
    assert row["content"] == "\u559c\u6b22\u5468\u672b\u722c\u5c71"
    assert json.loads(row["evidence_message_ids"]) == [turns[0][0]]
    assert get_memory_extraction_status(1, 2)["pending_turns"] == 0
