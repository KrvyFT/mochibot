"""Admin diary browsing helpers."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

import mochi.diary as diary_module
from mochi.diary import (
    DailyFile,
    list_diary_dates,
    list_diary_days,
    read_diary_day,
)


@pytest.fixture
def diary_files(tmp_path, monkeypatch):
    diary = DailyFile(
        path=tmp_path / "diary.md",
        label="Diary",
        max_lines=50,
        sections=("今日状態", "今日日記"),
        section_max_lines={"今日状態": 20, "今日日記": 30},
    )
    monkeypatch.setattr(diary_module, "TZ", timezone.utc)
    monkeypatch.setattr(
        diary_module, "_diary_date",
        lambda: datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(diary_module, "_today_str", lambda: "2026-09-05")
    monkeypatch.setattr(diary_module, "diary", diary)

    archive_dir = tmp_path / "diary_archive"
    archive_dir.mkdir()
    (archive_dir / "2026-09.md").write_text(
        "# Diary 2026-09-03\n\n## 今日状態\n\n## 今日日記\n"
        "周三路过花店。\n\n"
        "# Diary 2026-09-04\n\n## 今日状態\n\n## 今日日記\n"
        "周四交了作业。\n",
        encoding="utf-8",
    )
    diary.path.write_text(
        "# Diary 2026-09-05\n\n## 今日状態\n\n## 今日日記\n"
        "今天和心宿二聊了很久。\n",
        encoding="utf-8",
    )
    return diary, tmp_path


def test_list_diary_dates_and_pagination(diary_files):
    dates = list_diary_dates()
    assert dates == ["2026-09-05", "2026-09-04", "2026-09-03"]
    page1 = list_diary_days(page=1, limit=2)
    assert page1["total"] == 3
    assert page1["pages"] == 2
    assert [item["date"] for item in page1["items"]] == [
        "2026-09-05", "2026-09-04",
    ]
    page2 = list_diary_days(page=2, limit=2)
    assert [item["date"] for item in page2["items"]] == ["2026-09-03"]


def test_read_diary_day_skips_empty(diary_files, tmp_path):
    item = read_diary_day("2026-09-05")
    assert item is not None
    assert "心宿二" in item["content"]
    assert read_diary_day("2026-09-01") is None

    # Empty journal body should not count as a diary day.
    (tmp_path / "diary_archive" / "2026-08.md").write_text(
        "# Diary 2026-08-01\n\n## 今日状態\nok\n\n## 今日日記\n\n",
        encoding="utf-8",
    )
    assert "2026-08-01" not in list_diary_dates()
