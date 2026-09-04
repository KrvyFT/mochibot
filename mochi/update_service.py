"""Official-release discovery and out-of-process self-update support."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from mochi._version import read_version


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OFFICIAL_REPOSITORY = "shikidmsh-rgb/mochibot"
OFFICIAL_REMOTE_URL = f"https://github.com/{OFFICIAL_REPOSITORY}.git"
LATEST_RELEASE_URL = (
    f"https://api.github.com/repos/{OFFICIAL_REPOSITORY}/releases/latest"
)
UPDATE_REQUEST_PATH = PROJECT_ROOT / "data" / ".update_request"
UPDATE_RESULT_PATH = PROJECT_ROOT / "data" / ".update_result"

_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class UpdateError(RuntimeError):
    """A safe, owner-facing update failure."""


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str
    version: str
    name: str
    notes: str
    url: str
    current_version: str
    available: bool


def _run_git(*args: str, timeout: int = 60) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(PROJECT_ROOT),
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    output = (
        result.stdout
        if result.returncode == 0
        else (result.stdout or "") + (result.stderr or "")
    ).strip()
    return result.returncode, output


def _git_output(*args: str, timeout: int = 60) -> str:
    code, output = _run_git(*args, timeout=timeout)
    if code != 0:
        raise UpdateError(output or f"git {' '.join(args)} failed")
    return output.strip()


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = _TAG_RE.fullmatch(
        value.strip() if value.startswith("v") else f"v{value.strip()}"
    )
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _is_container() -> bool:
    return Path("/.dockerenv").exists() or bool(os.getenv("KUBERNETES_SERVICE_HOST"))


def validate_installation(
    *,
    require_clean: bool = True,
    require_launcher: bool = True,
) -> dict:
    """Return basic update capability for a local Git installation."""
    if _is_container():
        raise UpdateError("容器安装请由 Docker 更新镜像，Mochi 不会在容器内改写自己。")
    if require_launcher and os.getenv("MOCHIBOT_UPDATE_LAUNCHER") != "1":
        raise UpdateError("当前启动方式不支持自助更新，请通过 setup.bat 或 setup.sh 启动。")
    if not (PROJECT_ROOT / ".git").exists():
        raise UpdateError("当前不是 Git 安装，无法自动更新。")
    if _git_output("rev-parse", "--is-inside-work-tree").casefold() != "true":
        raise UpdateError("当前目录不是有效的 Git 工作区。")

    branch = _git_output("branch", "--show-current")
    dirty = _git_output("status", "--porcelain", "--untracked-files=normal")

    return {
        "branch": branch,
        "commit": _git_output("rev-parse", "--short", "HEAD"),
        "dirty": bool(dirty),
    }


async def check_for_update() -> ReleaseInfo:
    """Read GitHub's latest official release without changing local state."""
    validate_installation(require_clean=False, require_launcher=False)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"MochiBot/{read_version()}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(LATEST_RELEASE_URL, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise UpdateError(f"读取 GitHub Release 失败：{exc}") from exc

    tag = str(payload.get("tag_name") or "").strip()
    remote_version = _version_tuple(tag)
    if remote_version is None:
        raise UpdateError("GitHub 最新 Release 的版本标签格式无效。")

    current_version = read_version()
    current = _version_tuple(current_version)
    if current is None:
        raise UpdateError(f"本地版本号无效：{current_version}")

    notes = str(payload.get("body") or "").strip()
    return ReleaseInfo(
        tag=tag,
        version=tag.removeprefix("v"),
        name=str(payload.get("name") or tag).strip(),
        notes=notes[:4000],
        url=str(payload.get("html_url") or "").strip(),
        current_version=current_version,
        available=remote_version > current,
    )


def stage_update(
    release: ReleaseInfo,
    *,
    user_id: int,
    channel_id: int,
    transport: str,
) -> None:
    """Persist one exact release request after the owner's reply is delivered."""
    if not release.available or _version_tuple(release.tag) is None:
        raise UpdateError("没有可安装的新版本。")
    payload = {
        "tag": release.tag,
        "version": release.version,
        "user_id": int(user_id),
        "channel_id": int(channel_id),
        "transport": transport,
    }
    _write_json(UPDATE_REQUEST_PATH, payload)


def consume_update_request() -> dict | None:
    return _consume_json(UPDATE_REQUEST_PATH)


def record_update_result(request: dict, result: dict) -> None:
    payload = {
        "user_id": request.get("user_id", 0),
        "channel_id": request.get("channel_id", 0),
        "transport": request.get("transport", ""),
        **result,
    }
    _write_json(UPDATE_RESULT_PATH, payload)


def consume_update_result() -> dict | None:
    return _consume_json(UPDATE_RESULT_PATH)


def apply_pending_update(python_executable: str = sys.executable) -> dict | None:
    """Apply the staged release while the Mochi process is stopped."""
    request = consume_update_request()
    if request is None:
        return None

    tag = str(request.get("tag") or "")
    expected_version = str(request.get("version") or "")
    if _version_tuple(tag) is None or tag.removeprefix("v") != expected_version:
        result = {"ok": False, "message": "更新请求中的版本无效，未修改代码。"}
        record_update_result(request, result)
        return result

    try:
        validate_installation()
        _git_output(
            "fetch",
            "--force",
            OFFICIAL_REMOTE_URL,
            f"refs/tags/{tag}:refs/tags/{tag}",
            timeout=120,
        )
        target_hash = _git_output("rev-parse", f"refs/tags/{tag}^{{commit}}")
        _git_output("reset", "--hard", target_hash, timeout=120)
        if read_version() != expected_version:
            raise UpdateError("Release 标签与代码版本不一致，已停止更新。")

        install_error = _sync_requirements(python_executable)
        if install_error:
            raise UpdateError(f"依赖安装失败：{install_error}")

        for pycache in (PROJECT_ROOT / "mochi").rglob("__pycache__"):
            shutil.rmtree(pycache, ignore_errors=True)
        result = {
            "ok": True,
            "version": expected_version,
            "message": f"已经更新到 v{expected_version}，重新上线啦。",
        }
    except Exception as exc:
        result = {
            "ok": False,
            "message": f"更新失败：{exc}。",
        }

    record_update_result(request, result)
    return result


def _sync_requirements(python_executable: str) -> str:
    try:
        result = subprocess.run(
            [
                python_executable,
                "-m",
                "pip",
                "install",
                "-r",
                "requirements.txt",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(PROJECT_ROOT),
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if result.returncode == 0:
        return ""
    detail = ((result.stdout or "") + (result.stderr or "")).strip()
    return detail[-500:] or f"pip exited with code {result.returncode}"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    temp.replace(path)


def _consume_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        return payload
    except (OSError, ValueError):
        return None
    finally:
        path.unlink(missing_ok=True)
