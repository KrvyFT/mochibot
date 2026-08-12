"""Run-scoped tool definitions shared by provider schema and dispatch checks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class AvailableTool:
    """One immutable tool definition and how it entered the current run."""

    name: str
    definition_json: str
    source: str

    def definition(self) -> dict:
        return json.loads(self.definition_json)


@dataclass(frozen=True)
class ToolAvailability:
    """Immutable snapshot of tools executable in one Main tool loop."""

    entries: tuple[AvailableTool, ...] = ()

    @classmethod
    def from_definitions(
        cls,
        definitions: Iterable[dict],
        *,
        source: str,
    ) -> "ToolAvailability":
        return cls().with_definitions(definitions, source=source)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(entry.name for entry in self.entries)

    def allows(self, tool_name: object) -> bool:
        return isinstance(tool_name, str) and tool_name in self.names

    def provider_tools(self) -> list[dict]:
        """Return fresh mutable copies for provider adapters."""
        return [entry.definition() for entry in self.entries]

    def source_for(self, tool_name: str) -> str | None:
        for entry in self.entries:
            if entry.name == tool_name:
                return entry.source
        return None

    def with_definitions(
        self,
        definitions: Iterable[dict],
        *,
        source: str,
    ) -> "ToolAvailability":
        """Return a new snapshot with valid, genuinely new definitions appended."""
        existing = set(self.names)
        additions: list[AvailableTool] = []
        for definition in definitions:
            if not isinstance(definition, dict):
                continue
            function = definition.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name or name in existing:
                continue
            provider_definition = {
                key: value
                for key, value in definition.items()
                if not key.startswith("_")
            }
            try:
                definition_json = json.dumps(
                    provider_definition,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError):
                continue
            additions.append(AvailableTool(
                name=name,
                definition_json=definition_json,
                source=source,
            ))
            existing.add(name)
        if not additions:
            return self
        return ToolAvailability(self.entries + tuple(additions))


def unavailable_tool_error(tool_name: object) -> str:
    """Stable provider-facing error without revealing hidden registry contents."""
    return json.dumps({
        "ok": False,
        "error": "tool_not_available_this_turn",
        "tool": tool_name if isinstance(tool_name, str) else "",
        "hint": "Use request_tools first",
    }, ensure_ascii=False)
