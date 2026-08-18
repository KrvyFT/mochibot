"""Reminder skill — create, list, and cancel notify/self reminders."""

from datetime import datetime

from mochi.config import TZ
from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.skills.reminder.queries import (
    cancel_reminder,
    create_reminder,
    create_self_reminder,
    get_active_reminders,
    update_active_reminder,
)
from mochi.reminder_timer import notify_new_reminder


_RECURRENCES = {"daily", "weekdays", "weekly"}


def _bounded_summary(value: str, limit: int = 120) -> str:
    summary = " ".join(value.split())
    return summary if len(summary) <= limit else summary[:limit - 3] + "..."


def _current_owner_main(context: SkillContext) -> bool:
    if context.actor != "main":
        return False
    if (
        isinstance(context.user_id, bool)
        or not isinstance(context.user_id, int)
        or context.user_id < 0
    ):
        return False
    from mochi.config import OWNER_USER_ID
    return not OWNER_USER_ID or context.user_id == OWNER_USER_ID


class ReminderSkill(Skill):

    def init_schema(self, conn) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS reminders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                channel_id INTEGER NOT NULL DEFAULT 0,
                message    TEXT    NOT NULL,
                remind_at  TEXT    NOT NULL,
                fired      INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_reminders_pending
                ON reminders(fired, remind_at);
        """)
        from mochi.db import ensure_column
        ensure_column(conn, "reminders", "recurrence", "TEXT DEFAULT NULL")
        ensure_column(
            conn, "reminders", "status", "TEXT NOT NULL DEFAULT 'pending'",
        )
        ensure_column(conn, "reminders", "kind", "TEXT NOT NULL DEFAULT 'notify'")
        ensure_column(conn, "reminders", "context", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "source", "TEXT NOT NULL DEFAULT 'owner'")
        ensure_column(conn, "reminders", "transport", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "claimed_at", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "lease_until", "TEXT DEFAULT NULL")
        ensure_column(
            conn, "reminders", "attempt_count", "INTEGER NOT NULL DEFAULT 0",
        )
        ensure_column(conn, "reminders", "next_attempt_at", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "last_error", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "prepared_text", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "result_json", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "outcome", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "handled_at", "TEXT DEFAULT NULL")
        ensure_column(
            conn, "reminders", "delivery_cursor", "INTEGER NOT NULL DEFAULT 0",
        )
        ensure_column(
            conn, "reminders", "delivery_started_at", "TEXT DEFAULT NULL",
        )
        ensure_column(conn, "reminders", "delivered_at", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "cancelled_at", "TEXT DEFAULT NULL")
        conn.execute(
            "UPDATE reminders SET status = 'delivered' "
            "WHERE fired = 1 AND status = 'pending'"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminders_schedule "
            "ON reminders(kind, status, next_attempt_at, remind_at, lease_until)"
        )

    async def execute(self, context: SkillContext) -> SkillResult:
        args = context.args
        action = args.get("action", "list")
        uid = context.user_id

        if action == "create":
            kind = args.get("kind", "notify")
            if kind not in {"notify", "self"}:
                return SkillResult(
                    output=f"Invalid reminder kind: {kind!r}.",
                    success=False,
                )
            remind_at_raw = args.get("remind_at", "")
            if not remind_at_raw:
                return SkillResult(output="Need remind_at.", success=False)
            message = args.get("message", "")
            intent = args.get("intent", "")
            if kind == "notify":
                if not isinstance(message, str) or not message.strip():
                    return SkillResult(
                        output="Notify reminders need message and remind_at.",
                        success=False,
                    )
                message = message.strip()
            else:
                if not _current_owner_main(context):
                    return SkillResult(
                        output="Self reminders can only be created by Main.",
                        success=False,
                    )
                if not isinstance(intent, str) or not intent.strip():
                    return SkillResult(
                        output="Self reminders need intent and remind_at.",
                        success=False,
                    )
                if isinstance(message, str) and message.strip():
                    return SkillResult(
                        output="Self reminders accept intent, not a prewritten message.",
                        success=False,
                    )
                intent = intent.strip()

            remind_at, error = self._normalize_remind_at(remind_at_raw)
            if error:
                return error
            assert remind_at is not None
            recurrence, error = self._normalize_recurrence(
                args.get("recurrence", "one_time")
            )
            if error:
                return error
            if kind == "self":
                rid = create_self_reminder(
                    uid,
                    context.channel_id,
                    intent,
                    remind_at,
                    context.transport,
                    recurrence,
                )
                receipt = (
                    f"Self reminder #{rid} set for {remind_at}: "
                    f"{_bounded_summary(intent)}"
                )
            else:
                rid = create_reminder(
                    uid,
                    context.channel_id,
                    message,
                    remind_at,
                    recurrence,
                )
                receipt = f"Reminder #{rid} set for {remind_at}: {message}"
            if recurrence:
                receipt += f" (repeats {recurrence})"
            notify_new_reminder()
            return SkillResult(
                output=receipt, summary=receipt,
                entity_refs=[f"reminder:{rid}"], state_changed=True,
            )

        elif action == "list":
            reminders = get_active_reminders(uid)
            if not reminders:
                return SkillResult(output="No pending reminders.")
            lines = []
            for reminder in reminders:
                if reminder["kind"] == "self":
                    content = _bounded_summary(reminder.get("context") or "")
                    label = "Mochi 到时重新看看"
                else:
                    content = reminder["message"]
                    label = "提醒用户"
                recurrence_label = (
                    f"（{reminder['recurrence']}）"
                    if reminder.get("recurrence")
                    else ""
                )
                lines.append(
                    f"- #{reminder['id']} [{reminder['remind_at']}] "
                    f"{label}：{content}{recurrence_label}"
                )
            return SkillResult(
                output=f"{len(reminders)} reminders:\n" + "\n".join(lines)
            )

        elif action == "update":
            rid = args.get("reminder_id")
            try:
                rid = int(rid)
            except (TypeError, ValueError):
                return SkillResult(output="Need a valid reminder_id to update.", success=False)
            reminder = next(
                (item for item in get_active_reminders(uid) if item["id"] == rid),
                None,
            )
            if reminder is None:
                return SkillResult(output=f"Reminder #{rid} not found.", success=False)
            remind_at = None
            if "remind_at" in args:
                remind_at, error = self._normalize_remind_at(args.get("remind_at"))
                if error:
                    return error
            content_arg = "intent" if reminder["kind"] == "self" else "message"
            content = args.get(content_arg) if content_arg in args else None
            if content is not None:
                if not isinstance(content, str) or not content.strip():
                    return SkillResult(
                        output=f"{content_arg} must be non-empty when provided.",
                        success=False,
                    )
                content = content.strip()
            recurrence_provided = "recurrence" in args
            recurrence = None
            if recurrence_provided:
                recurrence, error = self._normalize_recurrence(
                    args.get("recurrence")
                )
                if error:
                    return error
            if remind_at is None and content is None and not recurrence_provided:
                return SkillResult(
                    output=(
                        f"Update reminder #{rid} with remind_at, "
                        f"{content_arg}, or recurrence."
                    ),
                    success=False,
                )
            update_kwargs = {
                "remind_at": remind_at,
                "content": content,
            }
            if recurrence_provided:
                update_kwargs["recurrence"] = recurrence
            updated = update_active_reminder(rid, uid, **update_kwargs)
            if not updated:
                return SkillResult(
                    output=f"Reminder #{rid} is already being delivered and cannot be updated.",
                    success=False,
                )
            notify_new_reminder()
            new_time = remind_at or reminder["remind_at"]
            new_content = content or (
                reminder.get("context")
                if reminder["kind"] == "self"
                else reminder["message"]
            )
            receipt = (
                f"Reminder #{rid} updated for {new_time}: "
                f"{_bounded_summary(new_content or '')}"
            )
            effective_recurrence = (
                recurrence
                if recurrence_provided
                else reminder.get("recurrence")
            )
            if effective_recurrence:
                receipt += f" (repeats {effective_recurrence})"
            return SkillResult(
                output=receipt,
                summary=receipt,
                entity_refs=[f"reminder:{rid}"],
                state_changed=True,
            )

        elif action == "delete":
            rid = args.get("reminder_id")
            if not rid:
                return SkillResult(output="Need reminder_id to delete.", success=False)
            try:
                deleted = cancel_reminder(int(rid), uid)
            except (ValueError, TypeError):
                return SkillResult(output=f"Invalid reminder_id: {rid}", success=False)
            if not deleted:
                return SkillResult(output=f"Reminder #{rid} not found.", success=False)
            notify_new_reminder()
            receipt = f"Reminder #{rid} deleted."
            return SkillResult(
                output=receipt, summary=receipt,
                entity_refs=[f"reminder:{rid}"], state_changed=True,
            )

        return SkillResult(output=f"Unknown action: {action}", success=False)

    @staticmethod
    def _normalize_remind_at(
        raw: object,
    ) -> tuple[str | None, SkillResult | None]:
        try:
            remind_at = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return None, SkillResult(
                output=f"Invalid remind_at format: {raw!r}. "
                       "Use ISO 8601, e.g. 2026-04-20T14:30:00+08:00",
                success=False,
            )
        if remind_at.tzinfo is None:
            remind_at = remind_at.replace(tzinfo=TZ)
        return remind_at.isoformat(), None

    @staticmethod
    def _normalize_recurrence(
        raw: object,
    ) -> tuple[str | None, SkillResult | None]:
        if raw in (None, "", "one_time"):
            return None, None
        if not isinstance(raw, str) or raw not in _RECURRENCES:
            return None, SkillResult(
                output=(
                    f"Invalid recurrence: {raw!r}. Use one_time, daily, "
                    "weekdays, or weekly."
                ),
                success=False,
            )
        return raw, None

    # ── Diary integration ─────────────────────────────────────

    def diary_status(self, user_id: int, today: str, now: datetime) -> list[str] | None:
        from mochi.db import _connect

        # Query unfired reminders for today (including future times)
        conn = _connect()
        rows = conn.execute(
            "SELECT message, remind_at, fired FROM reminders "
            "WHERE user_id = ? AND kind = 'notify' "
            "AND status IN ('pending', 'running', 'ready') "
            "AND remind_at >= ? AND remind_at < ? "
            "ORDER BY remind_at",
            (user_id, today, today + "T99"),
        ).fetchall()
        conn.close()

        if not rows:
            return None

        lines: list[str] = []
        for r in rows:
            try:
                remind_at = datetime.fromisoformat(r["remind_at"])
                if remind_at.tzinfo is None:
                    remind_at = remind_at.replace(tzinfo=TZ)
                time_str = remind_at.strftime("%H:%M")
                fired = bool(r["fired"]) or remind_at <= now
                mark = "✅" if fired else "⏳"
                lines.append(f"- {time_str} {r['message']} {mark}")
            except (ValueError, TypeError):
                pass

        return lines if lines else None
