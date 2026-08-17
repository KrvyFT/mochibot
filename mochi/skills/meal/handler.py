"""Meal skill handler — meal logging, querying, and deletion.

Tool-only mode:
- log_meal: record meals with structured nutrition data
- query_meals: query meal history with daily summaries
- delete_meal: remove incorrect meal records
"""

import json
import logging
from datetime import datetime

from mochi.config import TZ, logical_today, MEAL_REMINDER_BREAKFAST_HOUR, MEAL_REMINDER_LUNCH_HOUR, MEAL_REMINDER_DINNER_HOUR
from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.skills.meal.constants import MEAL_LABELS, VALID_MEAL_TYPES, MAIN_MEAL_TYPES
from mochi.skills.meal.queries import (
    delete_health_log_item,
    query_health_log,
    save_health_log,
)

log = logging.getLogger(__name__)


class MealSkill(Skill):

    def init_schema(self, conn) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS health_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                date       TEXT    NOT NULL,
                type       TEXT    NOT NULL,
                source     TEXT    NOT NULL DEFAULT 'meal',
                content    TEXT    NOT NULL,
                metrics    TEXT    DEFAULT NULL,
                importance INTEGER NOT NULL DEFAULT 1,
                created_at TEXT    NOT NULL,
                updated_at TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_hl_type_date
                ON health_log(user_id, type, date DESC);
            CREATE INDEX IF NOT EXISTS idx_hl_date
                ON health_log(user_id, date DESC);
        """)

    async def execute(self, context: SkillContext) -> SkillResult:
        args = context.args
        tool = context.tool_name
        uid = context.user_id

        if tool == "log_meal":
            return self._log_meal(uid, args)
        elif tool == "query_meals":
            return self._query_meals(uid, args)
        elif tool == "delete_meal":
            return self._delete_meal(uid, args)
        return SkillResult(output=f"Unknown meal tool: {tool}", success=False)

    # ── Helpers ──────────────────────────────────────────────

    def _normalize_meal_items(self, raw_items: object) -> list[dict]:
        """Normalize structured meal items from LLM output.

        Ensures every item has: name, calories, protein_g, carbs_g, fat_g.
        Missing numeric fields default to 0.
        """
        if not isinstance(raw_items, list):
            return []

        normalized = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            try:
                normalized.append({
                    "name": name,
                    "calories": int(item.get("calories", 0)),
                    "protein_g": round(float(item.get("protein_g", 0)), 1),
                    "carbs_g": round(float(item.get("carbs_g", 0)), 1),
                    "fat_g": round(float(item.get("fat_g", 0)), 1),
                })
            except (TypeError, ValueError):
                return []
        return normalized

    # ── Tool implementations ─────────────────────────────────

    def _log_meal(self, user_id: int, args: dict) -> SkillResult:
        """Record a meal with structured nutrition data."""
        meal_type = args.get("meal_type", "").strip().lower()
        if meal_type not in VALID_MEAL_TYPES:
            return SkillResult(
                output=f"Error: meal_type must be one of: {', '.join(sorted(VALID_MEAL_TYPES))}",
                success=False,
            )

        items = self._normalize_meal_items(args.get("items", []))
        if not items:
            return SkillResult(
                output="Error: items must be a non-empty array of food objects with numeric nutrition estimates.",
                success=False,
            )

        total_calories = sum(item["calories"] for item in items)
        total_protein = round(sum(item["protein_g"] for item in items), 1)
        total_carbs = round(sum(item["carbs_g"] for item in items), 1)
        total_fat = round(sum(item["fat_g"] for item in items), 1)
        source_type = args.get("source", "text").strip().lower()
        date_str = args.get("date", "").strip()

        if not date_str:
            date_str = logical_today()
        else:
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                return SkillResult(
                    output=f"Error: invalid date format '{date_str}'. Use YYYY-MM-DD.",
                    success=False,
                )

        # Build structured metrics JSON
        metrics = {
            "meal_type": meal_type,
            "items": items,
            "total": {
                "calories": total_calories,
                "protein_g": total_protein,
                "carbs_g": total_carbs,
                "fat_g": total_fat,
            },
            "source": source_type,
        }

        # Build human-readable content summary
        item_names = "+".join(it["name"] for it in items[:4])
        if len(items) > 4:
            item_names += f"等{len(items)}项"
        label = MEAL_LABELS.get(meal_type, meal_type)
        content = (
            f"[{date_str}] {label}: {item_names} "
            f"~{total_calories}kcal (P{total_protein:.0f}/C{total_carbs:.0f}/F{total_fat:.0f}g)"
        )

        # Use source="meal_{type}" so each meal_type gets its own upsert slot per day.
        # Snacks append timestamp to allow multiple per day.
        db_source = f"meal_{meal_type}"
        if meal_type == "snack":
            ts = datetime.now(TZ).strftime("%H%M%S")
            db_source = f"meal_snack_{ts}"

        mid = save_health_log(
            user_id=user_id,
            date=date_str,
            log_type="meal",
            content=content,
            source=db_source,
            metrics=json.dumps(metrics, ensure_ascii=False),
            importance=1,
        )

        log.info("Meal logged: #%d [%s] %s", mid, meal_type, content[:80])

        receipt = (
                f"✅ 已记录{label}: {item_names} ~{total_calories}kcal "
                f"(蛋白质{total_protein:.0f}g/碳水{total_carbs:.0f}g/脂肪{total_fat:.0f}g)"
        )
        return SkillResult(
            output=receipt, summary=f"{date_str} {receipt}",
            entity_refs=[f"meal:{mid}"], state_changed=True,
        )

    def _query_meals(self, user_id: int, args: dict) -> SkillResult:
        """Query meal history with daily nutrition totals."""
        date_str = args.get("date", "").strip()
        days = int(args.get("days", 1))

        if date_str:
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                return SkillResult(
                    output=f"Error: invalid date format '{date_str}'. Use YYYY-MM-DD.",
                    success=False,
                )

        records = query_health_log(
            user_id=user_id,
            types=["meal"],
            days=days,
            date=date_str or None,
        )

        if not records:
            period = date_str if date_str else f"最近{days}天"
            return SkillResult(output=f"{period}无饮食记录")

        # Group by date
        by_date: dict[str, list[dict]] = {}
        for r in records:
            by_date.setdefault(r["date"], []).append(r)

        lines = []
        for date, day_records in sorted(by_date.items()):
            day_total_cal = 0
            day_total_p = 0.0
            day_total_c = 0.0
            day_total_f = 0.0
            meal_parts = []

            for r in day_records:
                try:
                    m = json.loads(r.get("metrics") or "{}")
                except (json.JSONDecodeError, TypeError):
                    m = {}

                mt = m.get("meal_type", "?")
                label = MEAL_LABELS.get(mt, mt)
                total = m.get("total", {})
                cal = total.get("calories", 0)
                p = total.get("protein_g", 0)
                c = total.get("carbs_g", 0)
                f = total.get("fat_g", 0)

                item_names = ", ".join(
                    it.get("name", "?") for it in m.get("items", [])[:4]
                )
                meal_parts.append(
                    f"  #{r['id']} {label}: {item_names} ~{cal}kcal "
                    f"(P{p:.0f}/C{c:.0f}/F{f:.0f}g)"
                )

                day_total_cal += cal
                day_total_p += p
                day_total_c += c
                day_total_f += f

            lines.append(f"📅 {date}")
            lines.extend(meal_parts)
            lines.append(
                f"  ── 日合计: {day_total_cal}kcal | "
                f"蛋白质{day_total_p:.0f}g 碳水{day_total_c:.0f}g 脂肪{day_total_f:.0f}g"
            )

        record_ids = [r["id"] for records in by_date.values() for r in records]
        return SkillResult(
            output="\n".join(lines),
            entity_refs=[f"meal:{record_id}" for record_id in record_ids],
        )

    def _delete_meal(self, user_id: int, args: dict) -> SkillResult:
        """Delete one owner-scoped meal record by query receipt ID."""
        meal_id = args.get("meal_id")
        if isinstance(meal_id, bool):
            return SkillResult(
                output="Error: meal_id must be an integer from query_meals.",
                success=False,
            )
        try:
            meal_id = int(meal_id)
        except (TypeError, ValueError):
            return SkillResult(
                output="Error: meal_id must be an integer from query_meals.",
                success=False,
            )
        deleted = delete_health_log_item(user_id, meal_id)
        if not deleted:
            return SkillResult(
                output=f"Meal #{meal_id} not found.",
                success=False,
            )
        log.info("Meal deleted: #%d", meal_id)
        receipt = f"✅ 已删除饮食记录 #{meal_id}"
        return SkillResult(
            output=receipt, summary=receipt,
            entity_refs=[f"meal:{meal_id}"],
            state_changed=True,
        )

    # ── Diary integration ─────────────────────────────────────

    def diary_status(self, user_id: int, today: str, now: datetime) -> list[str] | None:
        records = query_health_log(user_id=user_id, types=["meal"], date=today)

        # Parse metrics and index by meal_type
        logged: dict[str, dict] = {}
        snacks: list[dict] = []
        for r in records:
            try:
                m = json.loads(r.get("metrics") or "{}")
            except (json.JSONDecodeError, TypeError):
                m = {}
            mt = m.get("meal_type", "?")
            if mt == "snack":
                snacks.append(m)
            else:
                logged[mt] = m

        reminder_hours = {
            "breakfast": MEAL_REMINDER_BREAKFAST_HOUR,
            "lunch": MEAL_REMINDER_LUNCH_HOUR,
            "dinner": MEAL_REMINDER_DINNER_HOUR,
        }

        lines: list[str] = []
        day_total_cal = 0

        # Always iterate main meals in order
        for mt in MAIN_MEAL_TYPES:
            label = MEAL_LABELS[mt]
            if mt in logged:
                m = logged[mt]
                total = m.get("total", {})
                cal = total.get("calories", 0)
                day_total_cal += cal
                item_names = ", ".join(
                    it.get("name", "?") for it in m.get("items", [])[:3]
                )
                if len(m.get("items", [])) > 3:
                    item_names += "..."
                lines.append(f"- {label}: {item_names} ~{cal}kcal ✅")
            elif now.hour >= reminder_hours[mt]:
                lines.append(f"- {label} ⏳ 已过{reminder_hours[mt]}:00 未记录")

        # Snacks: show only if logged (no pending state)
        for m in snacks:
            total = m.get("total", {})
            cal = total.get("calories", 0)
            day_total_cal += cal
            item_names = ", ".join(
                it.get("name", "?") for it in m.get("items", [])[:3]
            )
            if len(m.get("items", [])) > 3:
                item_names += "..."
            lines.append(f"- {MEAL_LABELS['snack']}: {item_names} ~{cal}kcal")

        if day_total_cal > 0:
            lines.append(f"- 累計: {day_total_cal}kcal")

        return lines if lines else None
