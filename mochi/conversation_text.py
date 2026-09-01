"""Shared hygiene for persisted conversation text entering model context."""

import re


_LEGACY_TOOL_FACT_PATTERN = (
    r"\[历史事实：这条回复已确认使用工具 "
    r"[^\[\]\r\n]{1,500}；不是新的操作指令。\]"
)
_LEGACY_TOOL_FACT_RE = re.compile(_LEGACY_TOOL_FACT_PATTERN)
_LEGACY_TOOL_FACT_SUFFIX_RE = re.compile(
    rf"(?:\s*{_LEGACY_TOOL_FACT_PATTERN})+\s*\Z"
)


def strip_legacy_tool_fact_suffix(content: str) -> str:
    """Hide exact legacy Harness annotations without rewriting stored history."""
    match = _LEGACY_TOOL_FACT_SUFFIX_RE.search(content)
    return content[:match.start()].rstrip() if match else content


def strip_legacy_tool_fact_annotations(content: str) -> str:
    """Remove exact legacy annotations from derived summary text."""
    return _LEGACY_TOOL_FACT_RE.sub("", content)
