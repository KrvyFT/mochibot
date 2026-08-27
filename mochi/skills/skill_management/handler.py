"""Skill Management — list / toggle / configure skills at runtime."""

import logging
import os
from copy import deepcopy

from mochi.skills.base import Skill, SkillContext, SkillResult

log = logging.getLogger(__name__)

_AGENT_CONFIG_FIELDS = {
    "heartbeat_interval_minutes": (
        "HEARTBEAT_INTERVAL_MINUTES",
        "int",
        5,
        240,
        "框架检查生活变化与调度任务的间隔；越短反应越及时，但不代表每次都会发消息。",
    ),
    "max_daily_proactive": (
        "MAX_DAILY_PROACTIVE",
        "int",
        0,
        50,
        "每天最多发送多少次主动消息。",
    ),
    "free_time_min_minutes": (
        "FREE_TIME_MIN_MINUTES",
        "int",
        30,
        1440,
        "两次 Free Time 之间随机等待的最短时间。",
    ),
    "free_time_max_minutes": (
        "FREE_TIME_MAX_MINUTES",
        "int",
        30,
        2880,
        "两次 Free Time 之间随机等待的最长时间。",
    ),
    "wake_earliest_hour": (
        "WAKE_EARLIEST_HOUR",
        "int",
        0,
        23,
        "每天最早进入清醒时段的本地小时。",
    ),
    "sleep_after_hour": (
        "SLEEP_AFTER_HOUR",
        "int",
        1,
        24,
        "每天从哪个本地小时起进入休息时段；24 表示午夜。",
    ),
    "timezone_offset_hours": (
        "TIMEZONE_OFFSET_HOURS",
        "float",
        -12,
        14,
        "当前所在地相对 UTC 的小时偏移，可使用 5.5 这类半小时时区。",
    ),
}


class SkillManagementSkill(Skill):

    def get_tools(self) -> list[dict]:
        definitions = deepcopy(super().get_tools())
        setting_keys = list(_AGENT_CONFIG_FIELDS)
        for definition in definitions:
            function = definition.get("function", {})
            if function.get("name") != "manage_agent_settings":
                continue
            changes = function["parameters"]["properties"]["changes"]
            changes["items"] = {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "enum": setting_keys,
                        "description": "要调整的设置",
                    },
                    "value": {
                        "type": "number",
                        "description": "新值",
                    },
                },
                "required": ["key", "value"],
                "additionalProperties": False,
            }
        return definitions

    async def execute(self, context: SkillContext) -> SkillResult:
        tool = context.tool_name
        args = context.args

        if tool == "list_skills":
            return self._list_skills()
        elif tool == "toggle_skill":
            return self._toggle_skill(args.get("skill_name", ""), args.get("enabled", True))
        elif tool == "get_skill_config":
            return self._get_skill_config(args.get("skill_name", ""))
        elif tool == "set_skill_config":
            return self._set_skill_config(
                args.get("skill_name", ""),
                args.get("key", ""),
                args.get("value", ""),
            )
        elif tool == "manage_agent_settings":
            action = args.get("action", "")
            if action == "view":
                return self._get_agent_config()
            if action == "set":
                return self._set_agent_configs(
                    context,
                    args.get("changes"),
                )
            return SkillResult(
                output="action 必须是 view 或 set。",
                success=False,
            )

        return SkillResult(output=f"Unknown tool: {tool}", success=False)

    # ── list_skills ──────────────────────────────────────────

    def _list_skills(self) -> SkillResult:
        from mochi.skills import get_skill_info_all

        infos = get_skill_info_all()
        # Sort: tool-type first, then alphabetically
        infos.sort(key=lambda s: (0 if s["type"] == "tool" else 1, s["name"]))

        lines = []
        for s in infos:
            if s["auto_disabled"]:
                missing = ", ".join(s["config_missing"])
                status = f"AUTO_OFF (缺: {missing})"
            elif s["admin_disabled"]:
                status = "OFF"
            else:
                status = "ON"

            tools_str = ", ".join(s["tools"]) if s["tools"] else "(none)"
            config_tag = " [has config]" if s["config_schema"] else ""
            lines.append(
                f"• {s['name']} [{status}] — {s['description']}\n"
                f"  type={s['type']}, tools: {tools_str}{config_tag}"
            )

        return SkillResult(
            output=f"Registered skills ({len(infos)}):\n\n" + "\n\n".join(lines),
        )

    # ── toggle_skill ─────────────────────────────────────────

    def _toggle_skill(self, skill_name: str, enabled: bool) -> SkillResult:
        from mochi.skills import get_skill, refresh_capability_summary
        from mochi.db import set_skill_enabled

        skill = get_skill(skill_name)
        if not skill:
            return SkillResult(output=f"Unknown skill: '{skill_name}'", success=False)

        # Core skills cannot be disabled
        if not enabled and getattr(skill, "core", False):
            return SkillResult(
                output=f"核心技能 '{skill_name}' 无法关闭。",
                success=False,
            )

        # Auto-disabled skills cannot be manually enabled
        if enabled and getattr(skill, "_config_missing", []):
            missing = ", ".join(skill._config_missing)
            return SkillResult(
                output=f"无法启用 '{skill_name}' — 缺少必要配置: {missing}。请先配置后重启。",
                success=False,
            )

        set_skill_enabled(skill_name, enabled)
        refresh_capability_summary()
        action = "已启用" if enabled else "已禁用"
        return SkillResult(output=f"技能 '{skill_name}' {action}，立即生效。")

    # ── get_skill_config ─────────────────────────────────────

    def _get_skill_config(self, skill_name: str) -> SkillResult:
        from mochi.skills import get_skill
        from mochi.db import get_skill_config
        from mochi.skill_config_resolver import _env_key

        skill = get_skill(skill_name)
        if not skill:
            return SkillResult(output=f"Unknown skill: '{skill_name}'", success=False)

        schema = skill._config_schema_typed
        if not schema:
            return SkillResult(output=f"技能 '{skill_name}' 没有可配置项。")

        db_overrides = get_skill_config(skill_name)
        # Keys that should be masked (internal or typically secret)
        secret_keys = {f.key for f in schema if f.internal}
        secret_keys |= set(getattr(skill, "requires_config", []))

        lines = [f"Config for '{skill_name}':\n"]
        for field in schema:
            if field.internal:
                continue

            env_name = _env_key(skill_name, field.key)
            db_val = db_overrides.get(field.key)
            env_val = os.getenv(env_name)

            if db_val is not None:
                source = "db"
            elif env_val is not None:
                source = "env"
            else:
                source = "default"

            current = skill.config.get(field.key, field.default)
            display = "***" if (field.key in secret_keys and current) else current
            lines.append(
                f"• {field.key} = {display} (source: {source}, type: {field.type})\n"
                f"  {field.description}\n"
                f"  default: {field.default}"
            )

        return SkillResult(output="\n\n".join(lines))

    # ── set_skill_config ─────────────────────────────────────

    def _set_skill_config(self, skill_name: str, key: str, value: str) -> SkillResult:
        from mochi.skills import get_skill, refresh_capability_summary
        from mochi.db import set_skill_config, delete_skill_config
        from mochi.skill_config_resolver import _cast

        skill = get_skill(skill_name)
        if not skill:
            return SkillResult(output=f"Unknown skill: '{skill_name}'", success=False)

        schema_map = {f.key: f for f in skill._config_schema_typed}
        if key not in schema_map:
            valid_keys = ", ".join(schema_map.keys()) if schema_map else "(none)"
            return SkillResult(
                output=f"技能 '{skill_name}' 没有配置项 '{key}'。可用: {valid_keys}",
                success=False,
            )

        # Empty value = clear DB override
        if not value:
            delete_skill_config(skill_name, key)
            skill.refresh_config()
            new_val = skill.config.get(key)
            refresh_capability_summary()
            return SkillResult(
                output=f"已清除 '{skill_name}.{key}' 的自定义值，当前使用: {new_val}",
            )

        # Validate type
        field = schema_map[key]
        try:
            _cast(value, field.type)
        except (ValueError, TypeError):
            return SkillResult(
                output=f"值 '{value}' 不符合类型 '{field.type}'。",
                success=False,
            )

        set_skill_config(skill_name, key, value)
        skill.refresh_config()
        new_val = skill.config.get(key)
        refresh_capability_summary()
        return SkillResult(
            output=f"已设置 '{skill_name}.{key}' = {new_val}（已保存到数据库，立即生效）",
        )

    def _get_agent_config(self) -> SkillResult:
        from mochi.admin.admin_db import get_system_config

        lines = ["你当前可调整的运行设置："]
        for key, (system_key, _type, minimum, maximum, description) in (
            _AGENT_CONFIG_FIELDS.items()
        ):
            lines.append(
                f"• {key} = {get_system_config(system_key)} "
                f"(范围 {minimum}–{maximum})\n  {description}"
            )
        return SkillResult(output="\n\n".join(lines))

    def _set_agent_configs(
        self,
        context: SkillContext,
        changes,
    ) -> SkillResult:
        from mochi.admin.admin_db import get_system_config, set_system_override

        if context.source != "chat":
            return SkillResult(
                output="只有用户当前对话可以授权调整运行设置。",
                success=False,
            )
        requested = changes if isinstance(changes, list) else []
        if not requested:
            return SkillResult(output="set 需要至少一项 changes。", success=False)

        current = {
            key: get_system_config(field[0])
            for key, field in _AGENT_CONFIG_FIELDS.items()
        }
        normalized_changes: dict[str, int | float] = {}
        for item in requested:
            if not isinstance(item, dict):
                return SkillResult(
                    output="changes 中的每一项都需要 key 和 value。",
                    success=False,
                )
            key = str(item.get("key") or "")
            value = item.get("value")
            field = _AGENT_CONFIG_FIELDS.get(key)
            if field is None:
                return SkillResult(
                    output=(
                        f"未知运行设置 '{key}'。可调整项："
                        + ", ".join(_AGENT_CONFIG_FIELDS)
                    ),
                    success=False,
                )
            if isinstance(value, bool):
                return SkillResult(
                    output=f"{key} 的值必须是数字。",
                    success=False,
                )
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return SkillResult(
                    output=f"{key} 的值必须是数字。",
                    success=False,
                )
            _, type_name, minimum, maximum, _ = field
            if type_name == "int":
                if not numeric.is_integer():
                    return SkillResult(
                        output=f"{key} 必须是整数。",
                        success=False,
                    )
                normalized: int | float = int(numeric)
            else:
                normalized = numeric
            if not minimum <= normalized <= maximum:
                return SkillResult(
                    output=f"{key} 必须在 {minimum}–{maximum} 之间。",
                    success=False,
                )
            normalized_changes[key] = normalized

        proposed = {**current, **normalized_changes}
        if proposed["free_time_min_minutes"] > proposed["free_time_max_minutes"]:
            return SkillResult(
                output="Free Time 最短间隔不能大于最长间隔。",
                success=False,
            )
        if proposed["wake_earliest_hour"] >= proposed["sleep_after_hour"]:
            return SkillResult(
                output="最早清醒时间必须早于休息时段起点。",
                success=False,
            )

        receipts = []
        changed = False
        for key, normalized in normalized_changes.items():
            old_value = current[key]
            if old_value == normalized:
                receipts.append(f"{key} 保持 {old_value}")
                continue
            system_key = _AGENT_CONFIG_FIELDS[key][0]
            set_system_override(system_key, str(normalized))
            new_value = get_system_config(system_key)
            receipts.append(f"{key}: {old_value} → {new_value}")
            changed = True
        return SkillResult(
            output=(
                "已调整运行设置：\n- "
                + "\n- ".join(receipts)
                + "\n新值会被后续 Heartbeat 循环读取；已经排定的下一次时刻"
                "不会追溯重算。"
            ),
            state_changed=changed,
        )
