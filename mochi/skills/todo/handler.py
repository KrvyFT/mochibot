"""Todo skill handler — execute logic only. Tool defs in SKILL.md."""

from datetime import datetime

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.skills.todo.queries import (
    complete_todo,
    create_todo,
    delete_todo,
    find_todos_by_exact_match,
    get_todos,
    reopen_todo,
    update_todo,
)


class TodoSkill(Skill):

    def init_schema(self, conn) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS todos (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                task       TEXT    NOT NULL,
                done       INTEGER NOT NULL DEFAULT 0,
                category   TEXT    NOT NULL DEFAULT '',
                created_at TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_todos_user
                ON todos(user_id, done);
        """)
        from mochi.db import ensure_column
        ensure_column(conn, "todos", "nudge_date", "TEXT DEFAULT NULL")
        ensure_column(conn, "todos", "source", "TEXT DEFAULT ''")
        ensure_column(conn, "todos", "completed_at", "TEXT DEFAULT NULL")

    async def execute(self, context: SkillContext) -> SkillResult:
        args = context.args
        action = args.get("action")
        uid = context.user_id

        if not action:
            return SkillResult(
                output=(
                    "Error: 'action' is required. Valid actions: "
                    "add, list, complete, reopen, update, delete."
                ),
                success=False)

        if action == "add":
            task = str(args.get("task") or "").strip()
            if not task:
                return SkillResult(output="Error: 'task' is required for add.", success=False)
            nudge_date = args.get("nudge_date")
            date_error = self._validate_nudge_date(nudge_date)
            if date_error:
                return date_error
            tid = create_todo(uid, task, nudge_date=nudge_date)
            nudge_str = f" (📅 {nudge_date} 提醒)" if nudge_date else ""
            receipt = f"Todo #{tid} added: '{task}'.{nudge_str}"
            return SkillResult(
                output=receipt, summary=receipt,
                entity_refs=[f"todo:{tid}"], state_changed=True,
            )

        elif action == "list":
            todos = get_todos(uid, include_done=args.get("include_done", False))
            if not todos:
                return SkillResult(output="No todos found.")
            lines = []
            for t in todos:
                mark = "✅" if t["done"] else "⬜"
                nudge = f" 📅{t['nudge_date']}" if t.get("nudge_date") else ""
                lines.append(f"#{t['id']} {mark} {t['task']}{nudge}")
            return SkillResult(output="\n".join(lines))

        elif action in {"complete", "reopen"}:
            resolved = self._resolve_todo(
                uid,
                args,
                action=action,
                done=(action == "reopen"),
            )
            if isinstance(resolved, SkillResult):
                return resolved
            todo_id = resolved
            ok = (
                complete_todo(uid, todo_id)
                if action == "complete"
                else reopen_todo(uid, todo_id)
            )
            receipt = (
                f"Todo #{todo_id} completed!"
                if ok
                else f"Todo #{todo_id} is not active or was not found."
            )
            if action == "reopen":
                receipt = (
                    f"Todo #{todo_id} reopened."
                    if ok
                    else f"Todo #{todo_id} is not completed or was not found."
                )
            return SkillResult(
                output=receipt, success=ok, summary=receipt if ok else "",
                entity_refs=[f"todo:{todo_id}"] if ok else [], state_changed=ok,
            )

        elif action == "delete":
            todo_id = self._parse_todo_id(args.get("todo_id"))
            if todo_id is None:
                return SkillResult(output="Error: 'todo_id' is required for delete.", success=False)
            ok = delete_todo(uid, todo_id)
            receipt = f"Todo #{todo_id} deleted." if ok else f"Todo #{todo_id} not found."
            return SkillResult(
                output=receipt, success=ok, summary=receipt if ok else "",
                entity_refs=[f"todo:{todo_id}"] if ok else [], state_changed=ok,
            )

        elif action == "update":
            resolved = self._resolve_todo(uid, args, action=action, done=None)
            if isinstance(resolved, SkillResult):
                return resolved
            todo_id = resolved
            fields = {}
            if "task" in args:
                task = str(args.get("task") or "").strip()
                if not task:
                    return SkillResult(
                        output="Error: task cannot be empty.",
                        success=False,
                    )
                fields["task"] = task
            if args.get("clear_nudge_date") is True:
                if "nudge_date" in args:
                    return SkillResult(
                        output=(
                            "Error: use either nudge_date or clear_nudge_date, "
                            "not both."
                        ),
                        success=False,
                    )
                fields["nudge_date"] = None
            elif "nudge_date" in args:
                date_error = self._validate_nudge_date(args["nudge_date"])
                if date_error:
                    return date_error
                fields["nudge_date"] = args["nudge_date"]
            if not fields:
                return SkillResult(
                    output=(
                        "Error: provide task, nudge_date, or "
                        "clear_nudge_date=true."
                    ),
                    success=False)
            update_status = update_todo(uid, todo_id, **fields)
            parts = ", ".join(f"{k}={v}" for k, v in fields.items())
            if update_status == "not_found":
                return SkillResult(
                    output=f"Todo #{todo_id} not found.",
                    success=False,
                )
            changed = update_status == "updated"
            receipt = (
                f"Todo #{todo_id} updated: {parts}."
                if changed
                else f"Todo #{todo_id} unchanged."
            )
            return SkillResult(
                output=receipt,
                summary=receipt,
                entity_refs=[f"todo:{todo_id}"],
                state_changed=changed,
            )

        return SkillResult(output=f"Unknown todo action: {action}", success=False)

    @staticmethod
    def _parse_todo_id(value: object) -> int | None:
        if isinstance(value, bool) or value in (None, ""):
            return None
        try:
            todo_id = int(value)
        except (TypeError, ValueError):
            return None
        return todo_id if todo_id > 0 else None

    def _resolve_todo(
        self,
        user_id: int,
        args: dict,
        *,
        action: str,
        done: bool | None,
    ) -> int | SkillResult:
        if args.get("todo_id") not in (None, ""):
            todo_id = self._parse_todo_id(args.get("todo_id"))
            if todo_id is None:
                return SkillResult(
                    output="Error: todo_id must be a positive integer.",
                    success=False,
                )
            return todo_id

        match = str(args.get("match") or "").strip()
        if not match:
            return SkillResult(
                output=f"Error: todo_id or match is required for {action}.",
                success=False,
            )

        matches = find_todos_by_exact_match(user_id, match, done=done)
        if len(matches) == 1:
            return matches[0]["id"]

        candidates = matches
        if not candidates:
            candidates = get_todos(user_id, include_done=True)
            if done is not None:
                candidates = [
                    todo for todo in candidates if todo["done"] is done
                ]
        candidate_lines = [
            (
                f"#{todo['id']} "
                f"{'✅' if todo['done'] else '⬜'} {todo['task']}"
            )
            for todo in candidates[:10]
        ]
        candidate_text = (
            "\n".join(candidate_lines) if candidate_lines else "(none)"
        )
        reason = "No exact match" if not matches else "Multiple exact matches"
        return SkillResult(
            output=(
                f"{reason} for {match!r}; no todo was changed.\n"
                f"Candidates:\n{candidate_text}"
            ),
            success=False,
        )

    @staticmethod
    def _validate_nudge_date(value: object) -> SkillResult | None:
        if value is None:
            return None
        if not isinstance(value, str):
            return SkillResult(
                output="Error: nudge_date must use YYYY-MM-DD.",
                success=False,
            )
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return SkillResult(
                output="Error: nudge_date must use YYYY-MM-DD.",
                success=False,
            )
        return None

    # ── Diary integration ─────────────────────────────────────

    def diary_status(self, user_id: int, today: str, now: datetime) -> list[str] | None:
        from mochi.skills.todo.queries import get_visible_todos

        todos = get_visible_todos(today)
        if not todos:
            return None

        lines: list[str] = []
        for t in todos:
            overdue = t.get("nudge_date") and t["nudge_date"] < today
            tag = " ⚠️逾期" if overdue else ""
            lines.append(f"- [ ] {t['task']} [todo_id={t['id']}]{tag}")
        return lines if lines else None
