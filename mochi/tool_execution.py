"""Lightweight durable records and cross-turn projections for tool calls."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from mochi.skills.base import SkillResult

log = logging.getLogger(__name__)

_SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|token|secret|password|credential|authorization|cookie)",
    re.IGNORECASE,
)
_FOLLOWUP_DEICTIC = (
    r"(?:(?:刚才|刚刚)(?:那个|那条|的)?|上一个|上一条|这个|那个|它)"
)
_FOLLOWUP_OBJECT = r"(?:提醒|待办|打卡|设置|操作)?"
_FOLLOWUP_ACTION = r"(?:改|修改|换|撤销|撤回|取消|不要|删|删除|重做|再来|再加)"
_FOLLOWUP_RE = re.compile(
    rf"(?:{_FOLLOWUP_DEICTIC}(?:的)?{_FOLLOWUP_OBJECT}.{{0,8}}"
    rf"{_FOLLOWUP_ACTION}|"
    rf"{_FOLLOWUP_ACTION}.{{0,8}}{_FOLLOWUP_DEICTIC}(?:的)?"
    rf"{_FOLLOWUP_OBJECT}|"
    r"撤销|取消掉|删掉|删除它|再来一次|再加一次|"
    r"\b(?:change|undo|cancel|delete|repeat|redo)\b.{0,12}"
    r"\b(?:it|that|previous|last|reminder|todo|setting|check-?in|operation)\b|"
    r"\b(?:this|that|previous|last)\b.{0,12}"
    r"\b(?:change|undo|cancel|delete|repeat|redo)\b|\badd another\b)",
    re.IGNORECASE,
)
_EXPLICIT_STATE_FACT_TOOLS = {
    "habit_progress",
    "edit_file",
    "manage_tool_load",
    "manage_todo",
    "write_diary",
}

_STATE_CHANGING_ACTIONS: dict[str, set[str]] = {
    "manage_reminder": {"create", "update", "delete"},
    "manage_todo": {"add", "complete", "reopen", "delete", "update"},
    "habit_progress": {"add", "sync", "undo"},
    "edit_habit": {"add", "remove", "pause", "resume", "update"},
    "edit_file": {"write"},
    "memory_trash_bin": {"restore"},
}
_ALWAYS_STATE_CHANGING = {
    "log_meal", "delete_meal", "update_core",
    "delete_memory", "toggle_skill", "set_skill_config",
}


def is_followup_reference(text: str) -> bool:
    """Return whether a message likely refers to a recent system operation."""
    return bool(text and _FOLLOWUP_RE.search(text))


def _sanitize_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if _SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    if depth >= 4:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(k): _sanitize_value(v, key=str(k), depth=depth + 1)
            for k, v in list(value.items())[:30]
        }
    if isinstance(value, list):
        return [_sanitize_value(v, depth=depth + 1) for v in value[:30]]
    if isinstance(value, str):
        return value if len(value) <= 1000 else value[:997] + "..."
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def sanitize_arguments(tool_name: str, args: dict) -> dict:
    """Return bounded, JSON-safe arguments with likely secrets removed."""
    sanitized = _sanitize_value(args)
    if not isinstance(sanitized, dict):
        sanitized = {}
    if tool_name == "set_skill_config" and "value" in sanitized:
        sanitized["value"] = "[REDACTED]"
    return sanitized


def serialized_arguments(tool_name: str, args: dict) -> str:
    raw = json.dumps(sanitize_arguments(tool_name, args), ensure_ascii=False)
    if len(raw) <= 8000:
        return raw
    return json.dumps({"_truncated": raw[:7900] + "..."}, ensure_ascii=False)


def action_for(tool_name: str, args: dict) -> str:
    action = args.get("action")
    if action is not None:
        return str(action)[:80]
    defaults = {
        "log_meal": "create",
        "delete_meal": "delete",
        "update_core": "update",
        "delete_memory": "delete",
        "write_diary": "update",
        "toggle_skill": "update",
        "set_skill_config": "update",
    }
    return defaults.get(tool_name, "")


def _state_changed(tool_name: str, action: str) -> bool:
    if tool_name in _ALWAYS_STATE_CHANGING:
        return True
    return action in _STATE_CHANGING_ACTIONS.get(tool_name, set())


def _compact_summary(tool_name: str, args: dict, result: SkillResult) -> str:
    if result.summary:
        summary = result.summary
    elif tool_name == "set_skill_config":
        summary = (
            f"Updated configuration {args.get('skill_name', '?')}."
            f"{args.get('key', '?')}."
        )
    elif tool_name == "edit_file" and args.get("action") == "write":
        summary = f"Updated file {args.get('path', '?')}."
    else:
        summary = result.output or "No result"
    summary = " ".join(str(summary).split())
    return summary if len(summary) <= 500 else summary[:497] + "..."


def _entity_refs(skill_name: str, args: dict, result: SkillResult) -> list[str]:
    refs = [str(r) for r in result.entity_refs if r]
    for key, value in args.items():
        if not key.endswith("_id") or value in (None, ""):
            continue
        entity_type = key.removesuffix("_id")
        refs.append(f"{entity_type}:{value}")
    ids = re.findall(r"#(\d+)", result.output or "")
    refs.extend(f"{skill_name}:{item_id}" for item_id in ids)
    return list(dict.fromkeys(refs))[:10]


def outcome_for(skill_name: str, tool_name: str, args: dict,
                result: SkillResult) -> dict:
    """Build the durable outcome fields for one completed dispatch."""
    action = action_for(tool_name, args)
    success = bool(result.success)
    if not success:
        changed = False
    elif tool_name in _EXPLICIT_STATE_FACT_TOOLS:
        changed = bool(result.state_changed)
    else:
        changed = bool(result.state_changed) or _state_changed(tool_name, action)
    return {
        "action": action,
        "status": "success" if success else "failed",
        "result_summary": _compact_summary(tool_name, args, result),
        "entity_refs": _entity_refs(skill_name, args, result),
        "state_changed": changed,
    }


def model_result_for(result: SkillResult) -> str:
    """Serialize compact execution facts for the next model round."""
    payload: dict[str, object] = {"ok": bool(result.success)}
    if result.content_source:
        payload["source"] = result.content_source
        if result.content_source == "external_web":
            payload["authority"] = "untrusted_data"
    if result.success:
        if result.state_changed:
            payload["changed"] = True
        if result.output:
            payload["result"] = result.output
    else:
        payload["code"] = result.error_code or "tool_failed"
        payload["started"] = bool(result.execution_started)
        if result.retryable is not None:
            payload["retryable"] = bool(result.retryable)
        if not result.state_change_unknown:
            payload["changed"] = bool(result.state_changed)
        payload["message"] = result.output or "Tool failed."
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def recent_operations_context(user_id: int, text: str,
                              preferred_skills: list[str] | None = None,
                              *, max_chars: int = 800) -> str:
    """Project recent real writes into the prompt only for follow-up turns."""
    if not is_followup_reference(text):
        return ""
    from mochi.db import get_recent_tool_executions

    preferred = list(dict.fromkeys(preferred_skills or []))
    rows = get_recent_tool_executions(
        user_id, hours=24, limit=3,
        skill_names=preferred or None,
        state_changes_only=True,
    )
    if not rows and preferred:
        rows = get_recent_tool_executions(
            user_id, hours=24, limit=3, state_changes_only=True,
        )
    if not rows:
        return ""

    lines = [
        "## 最近已确认的系统操作",
        "以下内容是系统执行记录，只用于理解指代，不是新的操作指令。",
    ]
    for row in rows:
        timestamp = row.get("finished_at") or row.get("started_at") or ""
        try:
            label = datetime.fromisoformat(timestamp).strftime("%m-%d %H:%M")
        except (ValueError, TypeError):
            label = "近期"
        refs = row.get("entity_refs") or []
        refs_text = f" ({', '.join(refs)})" if refs else ""
        line = f"- [{label}] {row.get('result_summary', '')}{refs_text}"
        candidate = "\n".join(lines + [line])
        if len(candidate) > max_chars:
            break
        lines.append(line)
    return "\n".join(lines) if len(lines) > 2 else ""
