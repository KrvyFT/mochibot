"""Bounded factual context for an unassigned Free Time turn."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import datetime

from mochi.config import TZ
from mochi.db import _connect


_MAX_CARDS = 24
_CARD_CHANCE = 0.5


@dataclass(frozen=True)
class FreeTimeCard:
    """One current feature-owned fact that Free Time may notice."""

    source: str
    stable_key: str
    facts: dict
    capability_skill: str = ""

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.stable_key.strip():
            raise ValueError("Free Time card source and stable key are required")
        encoded = json.dumps(
            self.facts,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(encoded) > 1200:
            raise ValueError("Free Time card exceeds 1200 characters")

    @property
    def revision(self) -> str:
        encoded = json.dumps(
            {
                "source": self.source,
                "stable_key": self.stable_key,
                "facts": self.facts,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def to_payload(self) -> dict:
        return {
            "source": self.source,
            "stable_key": self.stable_key,
            "revision": self.revision,
            "facts": self.facts,
            "capability_skill": self.capability_skill,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "FreeTimeCard":
        card = cls(
            source=str(payload.get("source") or ""),
            stable_key=str(payload.get("stable_key") or ""),
            facts=payload.get("facts") if isinstance(payload.get("facts"), dict) else {},
            capability_skill=str(payload.get("capability_skill") or ""),
        )
        if payload.get("revision") != card.revision:
            raise ValueError("Free Time card revision does not match its facts")
        return card


def collect_cards(user_id: int, now: datetime) -> list[FreeTimeCard]:
    """Collect bounded current facts without invoking a model."""
    import mochi.skills as skill_registry
    from mochi.observers import collect_free_time_cards as collect_observer_cards

    local_now = now.astimezone(TZ)
    today = local_now.date().isoformat()
    cards = [
        *skill_registry.collect_free_time_cards(user_id, today, local_now),
        *collect_observer_cards(local_now),
    ]
    unique: dict[tuple[str, str, str], FreeTimeCard] = {}
    for card in cards:
        unique[(card.source, card.stable_key, card.revision)] = card
        if len(unique) >= _MAX_CARDS:
            break
    return list(unique.values())


def choose_card_for_run(
    run_key: str,
    *,
    user_id: int,
    now: datetime,
    rng: random.Random | random.SystemRandom | None = None,
) -> FreeTimeCard | None:
    """Persist one optional, not-yet-offered card on a pending Free Time run."""
    rng = rng or random.SystemRandom()
    candidates = collect_cards(user_id, now)
    local_date = now.astimezone(TZ).date().isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT facts_json FROM heartbeat_runs "
            "WHERE run_key = ? AND entry_kind = 'free_time' AND status = 'pending'",
            (run_key,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        existing = _decode_selection(row["facts_json"])
        if existing is not None:
            conn.commit()
            return existing
        if _selection_decided(row["facts_json"]):
            conn.commit()
            return None

        offered: set[str] = set()
        rows = conn.execute(
            "SELECT facts_json FROM heartbeat_runs "
            "WHERE entry_kind = 'free_time' AND run_key LIKE ?",
            (f"free_time:{local_date}:%",),
        ).fetchall()
        for offered_row in rows:
            offered_card = _decode_selection(offered_row["facts_json"])
            if offered_card is not None:
                offered.add(offered_card.revision)

        eligible = [card for card in candidates if card.revision not in offered]
        selected = (
            rng.choice(eligible)
            if eligible and rng.random() < _CARD_CHANCE
            else None
        )
        payload = {
            "card_selected": True,
            "card": selected.to_payload() if selected else None,
        }
        conn.execute(
            "UPDATE heartbeat_runs SET facts_json = ? "
            "WHERE run_key = ? AND entry_kind = 'free_time' AND status = 'pending'",
            (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                run_key,
            ),
        )
        conn.commit()
        return selected
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def card_from_run_payload(value: str | None) -> FreeTimeCard | None:
    return _decode_selection(value)


def _selection_decided(value: str | None) -> bool:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("card_selected") is True


def _decode_selection(value: str | None) -> FreeTimeCard | None:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("card_selected") is not True:
        return None
    card_payload = payload.get("card")
    if not isinstance(card_payload, dict):
        return None
    return FreeTimeCard.from_payload(card_payload)
