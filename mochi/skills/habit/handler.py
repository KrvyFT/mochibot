"""Habit skill handler — recurring habit tracking with check-in, pause, and stats.

- SkillContext / SkillResult pattern
- TZ from mochi.config
- diary_status() for pluggable diary integration
"""

import logging
import sqlite3
from datetime import datetime, timedelta

from mochi.config import TZ, logical_today, logical_days_ago
from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.skills.habit.logic import parse_frequency, get_allowed_days
from mochi.skills.habit.queries import (
    add_habit,
    list_habits,
    deactivate_habit,
    update_habit,
    add_habit_checkins,
    reconcile_habit_total,
    get_habit_checkins,
    undo_latest_habit_checkin,
    get_habit_stats,
    get_habit_streak,
    pause_habit,
    resume_habit,
)

log = logging.getLogger(__name__)

_DAY_LABEL_CN = {0: "一", 1: "二", 2: "三", 3: "四", 4: "五", 5: "六", 6: "日"}
_VALID_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _format_allowed_days(days: set[int]) -> str:
    """Format allowed days as Chinese label, e.g. '六日'."""
    return "".join(_DAY_LABEL_CN[d] for d in sorted(days))


def _current_period(cycle: str) -> str:
    """Return the current period string: 'YYYY-MM-DD' for daily, 'YYYY-WNN' for weekly."""
    now = datetime.now(TZ)
    if cycle == "daily":
        return logical_today(now)
    else:
        return now.strftime("%G-W%V")


def _is_paused(habit: dict) -> bool:
    """Check if a habit is currently paused (paused_until >= today)."""
    paused_until = habit.get("paused_until")
    if not paused_until:
        return False
    today = logical_today()
    return paused_until >= today


def _build_frequency(
    cycle: object,
    target: object,
    weekdays: object = None,
) -> tuple[str | None, str | None]:
    if cycle not in {"daily", "weekly"}:
        return None, "cycle must be daily or weekly."
    if isinstance(target, bool):
        return None, "target must be a positive integer."
    try:
        target_value = int(target)
    except (TypeError, ValueError):
        return None, "target must be a positive integer."
    if target_value <= 0:
        return None, "target must be a positive integer."
    if weekdays is None:
        normalized_days: list[str] = []
    elif isinstance(weekdays, list):
        normalized_days = list(dict.fromkeys(
            str(day).strip().lower() for day in weekdays
        ))
    else:
        return None, "weekdays must be an array."
    if any(day not in _VALID_WEEKDAYS for day in normalized_days):
        return None, "weekdays may only contain mon, tue, wed, thu, fri, sat, or sun."
    if cycle == "daily" and normalized_days:
        return None, "weekdays can only be used with a weekly cycle."
    if normalized_days:
        return f"weekly_on:{','.join(normalized_days)}:{target_value}", None
    return f"{cycle}:{target_value}", None


def _frequency_fields(frequency: str) -> tuple[str, int, list[str]]:
    cycle, target = parse_frequency(frequency) or ("daily", 1)
    allowed = get_allowed_days(frequency)
    weekdays = (
        [_VALID_WEEKDAYS[index] for index in sorted(allowed)]
        if allowed is not None
        else []
    )
    return cycle, target, weekdays


def _habit_candidates(habits: list[dict]) -> str:
    if not habits:
        return "No active habits are available."
    choices = ", ".join(
        f"#{habit['id']} {habit['name']}" for habit in habits[:8]
    )
    return f"Active habits: {choices}."


def _resolve_active_habit(
    user_id: int,
    args: dict,
    action: str,
) -> tuple[dict | None, SkillResult | None]:
    habits = list_habits(user_id)
    raw_id = args.get("habit_id")
    raw_name = args.get("habit_name")
    name = str(raw_name).strip() if raw_name is not None else ""

    if raw_id not in (None, ""):
        try:
            habit_id = int(raw_id)
        except (TypeError, ValueError):
            return None, SkillResult(
                output="Error: habit_id must be an integer.",
                success=False,
            )
        habit = next((item for item in habits if item["id"] == habit_id), None)
        if habit is None:
            return None, SkillResult(
                output=f"Habit #{habit_id} not found. {_habit_candidates(habits)}",
                success=False,
            )
        if name and habit["name"] != name:
            return None, SkillResult(
                output=(
                    f"Error: habit_id #{habit_id} is '{habit['name']}', "
                    f"not '{name}'."
                ),
                success=False,
            )
        return habit, None

    if not name:
        return None, SkillResult(
            output=(
                f"Error: 'habit_id' or exact 'habit_name' is required "
                f"for {action}. {_habit_candidates(habits)}"
            ),
            success=False,
        )

    matches = [habit for habit in habits if habit["name"] == name]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, SkillResult(
            output=(
                f"Error: habit_name '{name}' is ambiguous. "
                f"{_habit_candidates(matches)} Use habit_id."
            ),
            success=False,
        )
    return None, SkillResult(
        output=(
            f"Habit named '{name}' not found. {_habit_candidates(habits)}"
        ),
        success=False,
    )


class HabitSkill(Skill):

    def init_schema(self, conn) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS habits (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                name        TEXT    NOT NULL,
                description TEXT    NOT NULL DEFAULT '',
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT    NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_habits_user_name
                ON habits(user_id, name);

            CREATE TABLE IF NOT EXISTS habit_logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                habit_id   INTEGER NOT NULL REFERENCES habits(id),
                user_id    INTEGER NOT NULL,
                logged_at  TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_habit_logs_habit
                ON habit_logs(habit_id, logged_at);
        """)
        from mochi.db import ensure_column
        for col, typedef in [
            ("frequency", "TEXT NOT NULL DEFAULT 'daily'"),
            ("category", "TEXT NOT NULL DEFAULT ''"),
            ("downstream", "TEXT NOT NULL DEFAULT ''"),
            ("importance", "TEXT NOT NULL DEFAULT 'normal'"),
            ("context", "TEXT NOT NULL DEFAULT ''"),
            ("paused_until", "TEXT DEFAULT NULL"),
            ("snoozed_until", "TEXT DEFAULT NULL"),
        ]:
            ensure_column(conn, "habits", col, typedef)
        ensure_column(conn, "habit_logs", "note", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "habit_logs", "period", "TEXT NOT NULL DEFAULT ''")

    async def execute(self, context: SkillContext) -> SkillResult:
        """Dispatch by tool_name + action."""
        args = context.args
        action = args.get("action", "")
        uid = context.user_id

        if context.tool_name == "habit_progress":
            if action == "list":
                return self._list(uid)
            elif action == "stats":
                return self._stats(uid, args)
            elif action == "add":
                return self._checkin(uid, args, mode="add")
            elif action == "sync":
                return self._checkin(uid, args, mode="sync")
            elif action == "undo":
                return self._undo_checkin(uid, args)
            return SkillResult(
                output=f"Unknown habit_progress action: {action}",
                success=False,
            )

        elif context.tool_name == "edit_habit":
            if action == "add":
                return self._add(uid, args)
            elif action == "remove":
                return self._remove(uid, args)
            elif action == "pause":
                return self._pause(uid, args)
            elif action == "resume":
                return self._resume(uid, args)
            elif action == "update":
                return self._update(uid, args)
            return SkillResult(output=f"Unknown edit_habit action: {action}", success=False)

        return SkillResult(output=f"Unknown habit tool: {context.tool_name}", success=False)

    def progress_context(self, user_id: int) -> str:
        """Return a concise owner-scoped progress snapshot for a routed turn."""
        habits = list_habits(user_id)
        if not habits:
            return "No active habits."

        now = datetime.now(TZ)
        today = logical_today(now)
        this_week = now.strftime("%G-W%V")
        weekday = now.weekday()
        lines: list[str] = []
        for habit in habits:
            parsed = parse_frequency(habit["frequency"])
            if not parsed:
                continue
            cycle, target = parsed
            period = today if cycle == "daily" else this_week
            done = len(get_habit_checkins(habit["id"], period))
            annotations: list[str] = []
            if _is_paused(habit):
                annotations.append(f"paused until {habit['paused_until']}")
            allowed = get_allowed_days(habit["frequency"])
            if allowed is not None and weekday not in allowed:
                annotations.append("not scheduled today")
            suffix = (
                f" ({'; '.join(annotations)})"
                if annotations
                else ""
            )
            lines.append(
                f"- #{habit['id']} {habit['name']} — {done}/{target}{suffix}"
            )
        return "\n".join(lines) if lines else "No valid active habits."

    # ── edit_habit actions ───────────────────────────────────────────────

    def _add(self, user_id: int, args: dict) -> SkillResult:
        name = args.get("name")
        if not name:
            return SkillResult(output="Error: 'name' is required for add.", success=False)
        cycle = args.get("cycle")
        if not cycle:
            return SkillResult(
                output="Error: 'cycle' is required for add.",
                success=False,
            )
        frequency, frequency_error = _build_frequency(
            cycle,
            args.get("target", 1),
            args.get("weekdays"),
        )
        if frequency_error:
            return SkillResult(output=f"Error: {frequency_error}", success=False)
        assert frequency is not None
        parsed = parse_frequency(frequency)
        assert parsed is not None
        cycle, target = parsed
        allowed_days = get_allowed_days(frequency)
        category = args.get("category", "")
        importance = args.get("importance", "normal")
        if importance not in ("important", "normal"):
            importance = "normal"
        context = args.get("context", "")

        try:
            hid, reactivated = add_habit(
                user_id=user_id, name=name, frequency=frequency,
                category=category, importance=importance, context=context,
            )
        except (sqlite3.IntegrityError, ValueError):
            return SkillResult(output=f"Error: habit '{name}' already exists.", success=False)

        if allowed_days is not None:
            cycle_label = f"every week on {_format_allowed_days(allowed_days)}"
        elif cycle == "daily":
            cycle_label = "daily"
        else:
            cycle_label = "weekly"
        imp_label = " ⚡important" if importance == "important" else ""
        ctx_label = f" ({context})" if context else ""
        verb = "reactivated" if reactivated else "created"
        return SkillResult(
            output=f"Habit #{hid} {verb}{imp_label}: {name} ({cycle_label} x{target}){ctx_label}"
                   f"{f' [{category}]' if category else ''}"
        )

    def _remove(self, user_id: int, args: dict) -> SkillResult:
        habit, error = _resolve_active_habit(user_id, args, "remove")
        if error:
            return error
        assert habit is not None
        habit_id = habit["id"]
        ok = deactivate_habit(user_id, habit_id)
        return SkillResult(
            output=f"Habit #{habit_id} deactivated." if ok else f"Habit #{habit_id} not found.",
            success=ok,
        )

    def _pause(self, user_id: int, args: dict) -> SkillResult:
        habit, error = _resolve_active_habit(user_id, args, "pause")
        if error:
            return error
        assert habit is not None
        habit_id = habit["id"]
        until = args.get("until", "")
        if not until:
            now = datetime.now(TZ)
            until = (datetime.strptime(logical_today(now), "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")
        try:
            datetime.strptime(until, "%Y-%m-%d")
        except ValueError:
            return SkillResult(output=f"Error: invalid date '{until}', use YYYY-MM-DD.", success=False)
        ok = pause_habit(user_id, habit_id, until)
        if not ok:
            return SkillResult(output=f"Habit #{habit_id} not found.", success=False)
        return SkillResult(output=f"⏸️ {habit['name']} paused until {until}.")

    def _resume(self, user_id: int, args: dict) -> SkillResult:
        habit, error = _resolve_active_habit(user_id, args, "resume")
        if error:
            return error
        assert habit is not None
        habit_id = habit["id"]
        ok = resume_habit(user_id, habit_id)
        if not ok:
            return SkillResult(output=f"Habit #{habit_id} not found.", success=False)
        return SkillResult(output=f"▶️ {habit['name']} resumed.")

    def _update(self, user_id: int, args: dict) -> SkillResult:
        habit, error = _resolve_active_habit(user_id, args, "update")
        if error:
            return error
        assert habit is not None
        habit_id = habit["id"]

        fields = {}
        for key in ("name", "context", "importance", "category"):
            if key in args and args[key] is not None:
                fields[key] = args[key]
        frequency_keys = {"cycle", "target", "weekdays"}
        if frequency_keys.intersection(args):
            current_cycle, current_target, current_weekdays = _frequency_fields(
                habit["frequency"],
            )
            cycle = args.get("cycle", current_cycle)
            target = args.get("target", current_target)
            weekdays = args.get(
                "weekdays",
                current_weekdays if cycle == current_cycle else [],
            )
            frequency, frequency_error = _build_frequency(
                cycle, target, weekdays,
            )
            if frequency_error:
                return SkillResult(
                    output=f"Error: {frequency_error}",
                    success=False,
                )
            fields["frequency"] = frequency
        if not fields:
            return SkillResult(
                output="Error: provide at least one field to update.",
                success=False,
            )

        # Validate importance if being updated
        if "importance" in fields and fields["importance"] not in ("important", "normal"):
            return SkillResult(output="Error: importance must be 'important' or 'normal'.", success=False)

        try:
            ok = update_habit(user_id, habit_id, **fields)
        except sqlite3.IntegrityError:
            return SkillResult(output=f"Error: habit name '{fields.get('name')}' already exists.", success=False)

        if not ok:
            return SkillResult(output=f"Habit #{habit_id} not found.", success=False)
        parts = ", ".join(f"{k}={v}" for k, v in fields.items())
        return SkillResult(output=f"Habit #{habit_id} updated: {parts}.")

    # ── habit_progress read actions ──────────────────────────────────────

    def _list(self, user_id: int) -> SkillResult:
        habits = list_habits(user_id)
        if not habits:
            return SkillResult(output="No active habits.")

        now = datetime.now(TZ)
        today = logical_today(now)
        # wall-clock 故意：ISO 周边界在 Mon 00:00，与 maintenance window (0-3) 不冲突
        this_week = now.strftime("%G-W%V")
        weekday = now.weekday()

        lines = []
        for h in habits:
            if _is_paused(h):
                lines.append(f"#{h['id']} ⏸️ {h['name']} — paused until {h['paused_until']}")
                continue
            parsed = parse_frequency(h["frequency"])
            if not parsed:
                continue
            cycle, target = parsed
            period = today if cycle == "daily" else this_week
            checkins = get_habit_checkins(h["id"], period)
            done = len(checkins)
            mark = "✅" if done >= target else "⬜"
            progress = f"{done}/{target}"
            imp = " ⚡" if h["importance"] == "important" else ""
            cat = f" [{h['category']}]" if h.get("category") else ""
            ctx = f" ({h['context']})" if h.get("context") else ""

            allowed = get_allowed_days(h["frequency"])
            day_hint = ""
            if allowed is not None:
                day_hint = f" 📅{_format_allowed_days(allowed)}"
                if weekday not in allowed:
                    day_hint += "(not active today)"

            streak_tag = ""
            if h["importance"] != "important":
                streak = get_habit_streak(h["id"], cycle, target, allowed)
                unit = "d" if cycle == "daily" else "w"
                streak_tag = f" 🔥{streak}{unit}" if streak > 0 else ""

            lines.append(f"#{h['id']} {mark} {h['name']}{imp}{cat}{ctx}{day_hint} — {progress}{streak_tag}")

        return SkillResult(output="\n".join(lines) if lines else "No active habits.")

    def _stats(self, user_id: int, args: dict) -> SkillResult:
        habit_id = args.get("habit_id")
        habits = list_habits(user_id)
        target_habits = [h for h in habits if (not habit_id or h["id"] == int(habit_id))]
        if not target_habits:
            return SkillResult(output="No habits found.")

        now = datetime.now(TZ)
        lines = []
        for h in target_habits:
            if _is_paused(h):
                lines.append(f"#{h['id']} {h['name']} — ⏸️ paused until {h['paused_until']}")
                continue
            parsed = parse_frequency(h["frequency"])
            if not parsed:
                continue
            cycle, target = parsed

            if cycle == "daily":
                periods = [logical_days_ago(i, now) for i in range(7)]
                stats = get_habit_stats(h["id"], periods)
                completed_days = sum(1 for p in periods if stats.get(p, 0) >= target)
                marks = "".join("✅" if stats.get(p, 0) >= target else "❌" for p in reversed(periods))
                streak_tag = ""
                if h["importance"] != "important":
                    allowed = get_allowed_days(h["frequency"])
                    streak = get_habit_streak(h["id"], cycle, target, allowed)
                    streak_tag = f" 🔥{streak}d streak" if streak > 0 else ""
                lines.append(f"#{h['id']} {h['name']} — 7d: {marks} ({completed_days}/7){streak_tag}")
            else:
                # wall-clock 故意：ISO 周边界在 Mon 00:00，与 maintenance window (0-3) 不冲突
                periods = [(now - timedelta(weeks=i)).strftime("%G-W%V") for i in range(4)]
                stats = get_habit_stats(h["id"], periods)
                completed_weeks = sum(1 for p in periods if stats.get(p, 0) >= target)
                marks = "".join("✅" if stats.get(p, 0) >= target else "❌" for p in reversed(periods))
                streak_tag = ""
                if h["importance"] != "important":
                    streak = get_habit_streak(h["id"], cycle, target)
                    streak_tag = f" 🔥{streak}w streak" if streak > 0 else ""
                lines.append(f"#{h['id']} {h['name']} — 4w: {marks} ({completed_weeks}/4){streak_tag}")

        return SkillResult(output="\n".join(lines) if lines else "No stats available.")

    # ── habit_progress write actions ─────────────────────────────────────

    def _checkin(
        self,
        user_id: int,
        args: dict,
        *,
        mode: str,
    ) -> SkillResult:
        habit, error = _resolve_active_habit(user_id, args, mode)
        if error:
            return error
        assert habit is not None
        habit_id = habit["id"]

        parsed = parse_frequency(habit["frequency"])
        if not parsed:
            return SkillResult(output=f"Error: invalid frequency on habit #{habit_id}.", success=False)
        cycle, target = parsed
        period = _current_period(cycle)
        note = args.get("note", "")
        raw_count = args.get("count")
        raw_total = args.get("total")
        if mode == "sync" and raw_total is None:
            return SkillResult(
                output="Error: total is required for sync.",
                success=False,
            )
        if mode == "sync" and raw_count is not None:
            return SkillResult(
                output="Error: count is only available for add.",
                success=False,
            )
        if mode == "add" and raw_total is not None:
            return SkillResult(
                output="Error: total is only available for sync.",
                success=False,
            )

        if mode == "sync":
            if (
                isinstance(raw_total, bool)
                or not isinstance(raw_total, int)
                or raw_total < 0
            ):
                return SkillResult(
                    output="Error: total must be a non-negative integer.",
                    success=False,
                )
            try:
                previous, actual = reconcile_habit_total(
                    habit_id, user_id, period, raw_total, note,
                )
            except ValueError:
                current = len(get_habit_checkins(habit_id, period))
                return SkillResult(
                    output=(
                        f"Error: reported total {raw_total} is below current "
                        f"progress {current}; clarify or undo check-ins first."
                    ),
                    success=False,
                )
            done = previous + actual
            if actual == 0:
                return SkillResult(
                    output=f"{habit['name']} is already at {done}/{target}.",
                )
        else:
            count = 1 if raw_count is None else raw_count
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count <= 0
            ):
                return SkillResult(
                    output="Error: count must be a positive integer.",
                    success=False,
                )
            actual = count
            done = add_habit_checkins(
                habit_id, user_id, period, actual, note,
            )
        remaining = max(0, target - done)

        extra = (
            f" (synced total, +{actual})"
            if mode == "sync"
            else f" (x{actual})" if actual > 1 else ""
        )
        if done >= target:
            return SkillResult(
                output=f"✅ {habit['name']} completed! ({done}/{target}) 🎉{extra}",
                state_changed=True,
            )
        cycle_label = "today" if cycle == "daily" else "this week"
        return SkillResult(
            output=(
                f"✅ {habit['name']} checked in {done}/{target}, "
                f"{remaining} left {cycle_label}{extra}"
            ),
            state_changed=True,
        )

    def _undo_checkin(self, user_id: int, args: dict) -> SkillResult:
        habit, error = _resolve_active_habit(user_id, args, "undo")
        if error:
            return error
        assert habit is not None
        habit_id = habit["id"]

        parsed = parse_frequency(habit["frequency"])
        if not parsed:
            return SkillResult(output=f"Error: invalid frequency on habit #{habit_id}.", success=False)
        cycle, target = parsed
        period = _current_period(cycle)
        remaining = undo_latest_habit_checkin(habit_id, user_id, period)
        if remaining is None:
            cycle_label = "today" if cycle == "daily" else "this week"
            return SkillResult(output=f"{habit['name']} has no checkins {cycle_label} — nothing to undo.")

        return SkillResult(
            output=f"↩️ {habit['name']} last checkin undone ({remaining}/{target})",
            state_changed=True,
        )

    # ── Diary integration ─────────────────────────────────────

    def diary_status(self, user_id: int, today: str, now: datetime) -> list[str] | None:
        habits = list_habits(user_id, active_only=True)
        if not habits:
            return None

        this_week = now.strftime("%G-W%V")
        weekday = now.weekday()
        lines: list[str] = []

        for h in habits:
            paused_until = h.get("paused_until")
            if paused_until and paused_until >= today:
                continue

            parsed = parse_frequency(h["frequency"])
            if not parsed:
                continue
            cycle, target = parsed
            period = today if cycle == "daily" else this_week

            checkins = get_habit_checkins(h["id"], period)
            done = len(checkins)

            allowed = get_allowed_days(h["frequency"])
            if allowed is not None and weekday not in allowed and done < target:
                continue

            name = h["name"]
            imp = "⚡" if h.get("importance") == "important" else ""
            ctx = h.get("context", "")
            ctx_tag = f" ({ctx})" if ctx else ""

            last_tag = ""
            if 0 < done < target and checkins:
                last_at = checkins[-1].get("logged_at")
                if last_at:
                    try:
                        t = datetime.fromisoformat(last_at)
                        last_tag = f" last:{t.strftime('%H:%M')}"
                    except (ValueError, TypeError):
                        pass

            if done >= target:
                lines.append(f"- {imp}{name} ({done}/{target}) ✅")
            else:
                lines.append(f"- {imp}{name} ({done}/{target}){ctx_tag}{last_tag} ⏳")

        return lines if lines else None
