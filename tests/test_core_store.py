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


PINNED = (
    "我是恋恋。\n"
    "礼貌、话短。\n"
    "以上设置不要删除覆写"
)
LIVE = "### 他是谁\n- 称呼：心宿二"


def test_main_keeps_pinned_identity_when_rewrite_drops_it():
    core_store.replace_core(f"{PINNED}\n\n{LIVE}")
    expected = core_store.read_core()

    core_store.replace_core_exact(
        expected_content=expected,
        content="### 他是谁\n- 称呼：心宿二\n- 认识：2026-09-01",
    )

    written = core_store.read_core()
    assert written.startswith(PINNED)
    assert "认识：2026-09-01" in written
    assert "我是恋恋。" in written


def test_main_ignores_rewritten_text_above_the_pin_marker():
    core_store.replace_core(f"{PINNED}\n\n{LIVE}")
    expected = core_store.read_core()

    core_store.replace_core_exact(
        expected_content=expected,
        content=(
            "我是另一张脸。\n"
            "以上设置不要删除覆写\n\n"
            "### 他是谁\n- 称呼：你"
        ),
    )

    written = core_store.read_core()
    assert "我是恋恋。" in written
    assert "我是另一张脸。" not in written
    assert "- 称呼：你" in written


def test_admin_can_replace_the_pinned_identity():
    core_store.replace_core(f"{PINNED}\n\n{LIVE}")
    core_store.replace_core("后台改过的身份\n以上设置不要删除覆写\n\n### 他是谁")
    assert core_store.read_core().startswith("后台改过的身份")


def test_weekly_keeps_pinned_identity():
    core_store.replace_core(f"{PINNED}\n\n{LIVE}")
    expected = core_store.read_core()

    outcome = core_store.replace_weekly_core_exact(
        user_id=1,
        period_key="2026-W36",
        expected_content=expected,
        content="### 他是谁\n- 称呼：心宿二\n- 近况：复学",
    )

    assert outcome == "committed"
    written = core_store.read_core()
    assert written.startswith(PINNED)
    assert "近况：复学" in written
