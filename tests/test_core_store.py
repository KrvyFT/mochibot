"""Atomic Core replacement contract."""

import pytest

import mochi.core_store as core_store


def test_agent_replaces_complete_core_with_internal_conflict_check():
    core_store.replace_core("alpha\n\nbeta")
    expected = core_store.read_core()

    core_store.replace_core_exact(
        expected_content=expected,
        content="gamma\n\nbeta",
    )
    assert core_store.read_core() == "gamma\n\nbeta"
    assert list((core_store.DATA_DIR / "core_history").glob("*.md"))

    core_store.replace_core("admin changed it")
    with pytest.raises(core_store.CoreConflictError):
        core_store.replace_core_exact(
            expected_content="gamma\n\nbeta",
            content="agent overwrote it",
        )
    assert core_store.read_core() == "admin changed it"
