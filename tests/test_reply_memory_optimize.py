"""Reply/memory optimization: overflow cap, high-signal extract, recall rank."""

from datetime import datetime, timedelta

from mochi.ai_client import (
    _fit_recalled_memories,
    _memory_freshness_score,
    _remember_recalled_memory_ids,
    _retrieve_memories_for_turn,
    _suppressed_memory_ids,
    _user_recent_memory_ids,
)
from mochi.db import (
    _connect,
    get_conversation_context,
    save_message,
)
from mochi.memory_extraction import (
    HIGH_SIGNAL_BATCH_SIZE,
    _batch_has_high_signal,
    _extraction_threshold,
)
from mochi.relationship_voice import compact_voice_summary, starting_voice
from mochi.skills.relationship_health.handler import RelationshipHealthSkill


def test_overflow_is_capped_when_summary_lags(monkeypatch):
    monkeypatch.setattr("mochi.db.CONV_OVERFLOW_MAX_MESSAGES", 4)
    monkeypatch.setattr("mochi.db.CONV_OVERFLOW_MAX_TOKENS", 5000)

    for i in range(12):
        save_message(1, "user", f"user turn {i} " + ("x" * 20), turn_id=f"t{i}")
        save_message(
            1, "assistant", f"assistant turn {i}", turn_id=f"t{i}",
        )

    # Materialize summary state, then leave the cursor behind recent turns.
    get_conversation_context(1, recent_turns=2)
    conn = _connect()
    conn.execute(
        "UPDATE conversation_summary_state SET through_message_id = 0, "
        "summary = 'early' WHERE user_id = 1",
    )
    conn.commit()
    conn.close()

    context = get_conversation_context(1, recent_turns=2)
    assert context["summary"] == "early"
    assert len(context["overflow"]) <= 4
    assert context["overflow"]
    # Newest overflow should be retained.
    joined = " ".join(m["content"] for m in context["overflow"])
    assert "user turn 9" in joined or "assistant turn 9" in joined


def test_high_signal_batch_detection_and_threshold():
    quiet = [{
        "user": {"content": "今天天气怎样"},
        "assistant": {"content": "还行"},
    }]
    loud = [{
        "user": {"content": "记得帮我吃药，我对花生过敏"},
        "assistant": {"content": "好"},
    }]
    assert not _batch_has_high_signal(quiet)
    assert _batch_has_high_signal(loud)
    assert HIGH_SIGNAL_BATCH_SIZE <= 5


def test_extraction_threshold_drops_on_high_signal(monkeypatch):
    monkeypatch.setattr(
        "mochi.memory_extraction.get_memory_extraction_pending_turns",
        lambda *_a, **_k: [{
            "user": {"content": "我喜欢清淡，别忘了提醒我吃药"},
            "assistant": {"content": "嗯"},
        }] * 4,
    )
    assert _extraction_threshold(1, pending_turns=4) == HIGH_SIGNAL_BATCH_SIZE
    monkeypatch.setattr(
        "mochi.memory_extraction.get_memory_extraction_pending_turns",
        lambda *_a, **_k: [{
            "user": {"content": "随便聊聊"},
            "assistant": {"content": "好"},
        }] * 4,
    )
    from mochi.memory_extraction import EXTRACTION_BATCH_SIZE
    assert _extraction_threshold(1, pending_turns=4) == EXTRACTION_BATCH_SIZE


def test_memory_item_cooldown_suppresses_same_id_only():
    _user_recent_memory_ids.clear()
    _remember_recalled_memory_ids(7, [101, 102])
    suppressed = _suppressed_memory_ids(7, cooldown_s=60.0)
    assert suppressed == {101, 102}
    assert _suppressed_memory_ids(8, cooldown_s=60.0) == set()


def test_recall_sort_prefers_importance_and_freshness(monkeypatch):
    now = datetime.now().isoformat()
    old = (datetime.now() - timedelta(days=90)).isoformat()
    items = [
        {
            "id": 1, "content": "旧低重要", "importance": 1,
            "score": 8.0, "vec_sim": 0.9, "has_vector": True,
            "fts_hit": True, "match_source": "fts",
            "updated_at": old, "created_at": old,
            "evidence_start": "", "evidence_end": "",
        },
        {
            "id": 2, "content": "新高重要", "importance": 3,
            "score": 5.0, "vec_sim": 0.5, "has_vector": True,
            "fts_hit": True, "match_source": "fts",
            "updated_at": now, "created_at": now,
            "evidence_start": "", "evidence_end": "",
        },
    ]

    monkeypatch.setattr(
        "mochi.ai_client._memory_recall_queries",
        lambda text, *_a, **_k: [("current", text)],
    )
    monkeypatch.setattr(
        "mochi.ai_client.recall_memory",
        lambda *_a, **_k: items,
    )
    monkeypatch.setattr(
        "mochi.config.MEMORY_AUTO_RECALL", True,
    )
    monkeypatch.setattr(
        "mochi.config.MEMORY_AUTO_RECALL_TOP_K", 5,
    )
    monkeypatch.setattr(
        "mochi.config.MEMORY_AUTO_RECALL_MAX_ITEMS", 3,
    )
    monkeypatch.setattr(
        "mochi.config.MEMORY_AUTO_RECALL_MIN_VEC_SIM", 0.1,
    )
    monkeypatch.setattr(
        "mochi.config.MEMORY_AUTO_RECALL_MAX_CHARS", 200,
    )
    monkeypatch.setattr(
        "mochi.config.MEMORY_AUTO_RECALL_MAX_TOKENS", 800,
    )
    monkeypatch.setattr(
        "mochi.config.MEMORY_AUTO_RECALL_COOLDOWN", 0,
    )
    monkeypatch.setattr("mochi.config.KG_ENABLED", False)

    class _Pool:
        def embed_batch(self, texts):
            return [None] * len(texts)

    monkeypatch.setattr(
        "mochi.model_pool.get_pool", lambda: _Pool(),
    )

    selected = _retrieve_memories_for_turn("过敏药", user_id=1)
    assert selected
    assert selected[0]["memory_id"] == 2
    assert _memory_freshness_score(now) > _memory_freshness_score(old)


def test_fit_recalled_memories_respects_item_cap():
    candidates = [
        {"text": f"m{i}", "candidate_id": f"memory:{i}"}
        for i in range(5)
    ]
    fitted = _fit_recalled_memories(candidates, max_tokens=10_000, max_items=2)
    assert len(fitted) == 2


def test_compact_voice_for_free_time(monkeypatch):
    monkeypatch.setattr(
        "mochi.skills.relationship_health.handler.OWNER_USER_ID", 1,
        raising=False,
    )
    monkeypatch.setattr(
        "mochi.config.OWNER_USER_ID", 1,
    )
    skill = RelationshipHealthSkill()
    compact = skill.prompt_section(compact=True)
    full = skill.prompt_section(compact=False)
    assert "Free Time" in compact or "短开场" in compact
    assert "行为准则" in full or full == starting_voice().strip()
    assert len(compact) < len(full) or "摘要" in compact_voice_summary()
