"""Focused safety and handoff tests for owner-requested self-update."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from mochi.ai_client import ChatResult
from mochi.skills.base import SkillContext
from mochi.update_service import ReleaseInfo, UpdateError


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(".env\ndata/\n", encoding="utf-8")
    (repo / "requirements.txt").write_text("", encoding="utf-8")
    package = repo / "mochi"
    package.mkdir()
    (package / "__init__.py").write_text(
        '__version__ = "1.0.2"\n',
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")


def test_validate_installation_accepts_only_clean_official_main(
    tmp_path,
    monkeypatch,
):
    import mochi.update_service as updater

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "remote", "add", "origin", updater.OFFICIAL_REMOTE_URL)
    monkeypatch.setattr(updater, "PROJECT_ROOT", repo)
    monkeypatch.setattr(updater, "_is_container", lambda: False)

    status = updater.validate_installation(require_launcher=False)
    assert status["branch"] == "main"
    assert not status["dirty"]

    (repo / "local.py").write_text("mine\n", encoding="utf-8")
    with pytest.raises(UpdateError, match="本地代码改动"):
        updater.validate_installation(require_launcher=False)
    assert updater.validate_installation(
        require_clean=False,
        require_launcher=False,
    )["dirty"]


@pytest.mark.asyncio
async def test_install_is_armed_only_after_reply_delivery(monkeypatch):
    import mochi.skills.system_update.handler as handler
    import mochi.shutdown as shutdown

    release = ReleaseInfo(
        tag="v1.1.0",
        version="1.1.0",
        name="v1.1.0",
        notes="",
        url="https://github.com/shikidmsh-rgb/mochibot/releases/tag/v1.1.0",
        current_version="1.0.2",
        available=True,
    )

    async def fake_check():
        return release

    staged = []
    exits = []
    monkeypatch.setattr(handler, "check_for_update", fake_check)
    monkeypatch.setattr(handler, "validate_installation", lambda **kwargs: {})
    monkeypatch.setattr(
        handler,
        "stage_update",
        lambda value, **context: staged.append((value, context)),
    )
    monkeypatch.setattr(
        shutdown,
        "request_process_exit",
        lambda code: exits.append(code),
    )

    skill = handler.SystemUpdateSkill()
    result = await skill.execute(SkillContext(
        trigger="tool_call",
        user_id=1,
        channel_id=99,
        transport="telegram",
        actor="main",
        source="chat",
        turn_id="turn-1",
        tool_name="install_system_update",
        args={},
    ))

    assert result.success
    assert staged[0][0] == release
    assert staged[0][1]["channel_id"] == 99
    assert exits == []

    delivered = ChatResult(
        text="马上更新。",
        _after_delivery=[result.after_delivery],
    )
    assert delivered.confirm_delivered()
    assert exits == [shutdown.UPDATE_EXIT_CODE]


@pytest.mark.asyncio
async def test_autonomous_runtime_cannot_install(monkeypatch):
    from mochi.skills.system_update.handler import SystemUpdateSkill

    skill = SystemUpdateSkill()
    result = await skill.execute(SkillContext(
        trigger="tool_call",
        user_id=1,
        actor="main",
        source="runtime:attention",
        tool_name="install_system_update",
        args={},
    ))

    assert not result.success
    assert "主人当前对话" in result.output


def test_launcher_fast_forwards_exact_staged_release(tmp_path, monkeypatch):
    import mochi.update_service as updater

    repo = tmp_path / "repo"
    _init_repo(repo)
    first_hash = _git(repo, "rev-parse", "HEAD")
    init_file = repo / "mochi" / "__init__.py"
    init_file.write_text('__version__ = "1.1.0"\n', encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "release")
    _git(repo, "tag", "v1.1.0")
    release_hash = _git(repo, "rev-parse", "HEAD")
    _git(repo, "reset", "--hard", first_hash)
    _git(repo, "remote", "add", "origin", str(repo))
    (repo / ".env").write_text("ADMIN_TOKEN=keep-me\n", encoding="utf-8")
    data_dir = repo / "data"
    data_dir.mkdir()
    user_data = data_dir / "user.db"
    user_data.write_bytes(b"owner data")

    monkeypatch.setattr(updater, "PROJECT_ROOT", repo)
    monkeypatch.setattr(updater, "OFFICIAL_REPOSITORY", str(repo).casefold())
    monkeypatch.setattr(updater, "UPDATE_REQUEST_PATH", repo / "data" / ".update_request")
    monkeypatch.setattr(updater, "UPDATE_RESULT_PATH", repo / "data" / ".update_result")
    monkeypatch.setattr(updater, "_is_container", lambda: False)
    monkeypatch.setenv("MOCHIBOT_UPDATE_LAUNCHER", "1")
    monkeypatch.setattr(
        updater,
        "read_version",
        lambda: (
            init_file.read_text(encoding="utf-8").split('"')[1]
        ),
    )

    real_run = subprocess.run

    def run_with_fake_pip(command, **kwargs):
        if command[1:4] == ["-m", "pip", "install"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        return real_run(command, **kwargs)

    monkeypatch.setattr(updater.subprocess, "run", run_with_fake_pip)
    updater.stage_update(
        ReleaseInfo(
            tag="v1.1.0",
            version="1.1.0",
            name="v1.1.0",
            notes="",
            url="",
            current_version="1.0.2",
            available=True,
        ),
        user_id=1,
        channel_id=99,
        transport="telegram",
    )

    result = updater.apply_pending_update("python")

    assert result["ok"]
    assert _git(repo, "rev-parse", "HEAD") == release_hash
    assert (repo / ".env").read_text(encoding="utf-8") == "ADMIN_TOKEN=keep-me\n"
    assert user_data.read_bytes() == b"owner data"
    assert updater.consume_update_result()["version"] == "1.1.0"


def test_launcher_adds_project_root_when_run_from_scripts_directory():
    project_root = Path(__file__).resolve().parent.parent
    command = (
        "import runpy; "
        "runpy.run_path('start.py'); "
        "import mochi.update_service"
    )

    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=project_root / "scripts",
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_process_exit_request_survives_early_startup(monkeypatch):
    import mochi.shutdown as shutdown

    monkeypatch.setattr(shutdown, "_restart_event", None)
    monkeypatch.setattr(shutdown, "_exit_requested", False)
    monkeypatch.setattr(
        shutdown,
        "_requested_exit_code",
        shutdown.RESTART_EXIT_CODE,
    )

    shutdown.request_process_exit(shutdown.UPDATE_EXIT_CODE)
    event = shutdown.init_restart_event()

    assert event.is_set()
    assert shutdown.requested_exit_code() == shutdown.UPDATE_EXIT_CODE
