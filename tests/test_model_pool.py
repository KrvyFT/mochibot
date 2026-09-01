"""Embedding batch ordering contract and gateway-quirk reporting."""

import logging

import pytest

import mochi.model_pool as model_pool
from mochi.model_pool import _coerce_batch_index, _resolve_batch_order


class _Item:
    """Minimal stand-in for an item in an OpenAI embedding response."""

    def __init__(self, index):
        self.index = index


@pytest.fixture(autouse=True)
def forget_reported_quirks():
    """Quirk reporting dedups per process, so isolate it between tests."""
    model_pool._reported_index_quirks.clear()
    yield
    model_pool._reported_index_quirks.clear()


def test_valid_indexes_are_honoured_as_a_permutation():
    assert _resolve_batch_order([_Item(1), _Item(0)], 2) == [1, 0]
    # String indexes are what several compatible gateways actually send.
    assert _resolve_batch_order([_Item("1"), _Item("0")], 2) == [1, 0]


def test_size_mismatch_is_not_recoverable():
    with pytest.raises(ValueError, match="batch size mismatch"):
        _resolve_batch_order([_Item(0)], 2)


def test_constant_indexes_announce_the_fallback_only_once(caplog):
    with caplog.at_level(logging.DEBUG, logger="mochi.model_pool"):
        assert _resolve_batch_order([_Item(0), _Item(0)], 2) == [0, 1]
        assert _resolve_batch_order([_Item(0)] * 3, 3) == [0, 1, 2]

    levels = [record.levelno for record in caplog.records]
    assert levels.count(logging.INFO) == 1
    assert logging.WARNING not in levels


def test_absent_indexes_are_treated_as_a_gateway_trait(caplog):
    with caplog.at_level(logging.DEBUG, logger="mochi.model_pool"):
        assert _resolve_batch_order([_Item(None), _Item(None)], 2) == [0, 1]

    assert [record.levelno for record in caplog.records] == [logging.INFO]


def test_contradictory_indexes_keep_warning(caplog):
    with caplog.at_level(logging.DEBUG, logger="mochi.model_pool"):
        # A gateway that populates indexes but disagrees with itself may have
        # shuffled the payload too, so response order is a guess here.
        assert _resolve_batch_order([_Item(1), _Item(2)], 2) == [0, 1]
        assert _resolve_batch_order([_Item(0), _Item(None)], 2) == [0, 1]

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2


def test_coerce_batch_index_rejects_non_indexes():
    assert _coerce_batch_index(3) == 3
    assert _coerce_batch_index(" 3 ") == 3
    assert _coerce_batch_index(True) is None
    assert _coerce_batch_index(None) is None
    assert _coerce_batch_index("first") is None
    assert _coerce_batch_index(1.5) is None
