"""E2E tests for the chat flow: message → LLM → tool dispatch → DB → response."""

import pytest

from mochi.transport import IncomingMessage
from mochi.ai_client import chat
from mochi.db import get_recent_tool_executions
from mochi.skills.todo.queries import get_todos
from tests.e2e.mock_llm import make_response, make_tool_call


def _msg(text: str, user_id: int = 1, channel_id: int = 100) -> IncomingMessage:
    """Helper to create an IncomingMessage."""
    return IncomingMessage(
        user_id=user_id, channel_id=channel_id,
        text=text, transport="fake",
    )


class TestSimpleReply:
    """LLM returns a plain text reply — no tool calls."""

    @pytest.mark.asyncio
    async def test_simple_reply(self, mock_llm_factory):
        mock = mock_llm_factory([make_response("Hello there!")])

        reply = await chat(_msg("Hi"))

        assert reply.text == "Hello there!"
        assert len(mock.call_log) == 1

    @pytest.mark.asyncio
    async def test_update_core(self, mock_llm_factory):
        from mochi.core_store import read_core, replace_core
        replace_core("Core anchor")
        mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("update_core", {
                    "action": "insert_after",
                    "anchor_text": "Core anchor",
                    "content": "User likes jasmine tea",
                }),
            ]),
            # Round 2: LLM gives final reply after tool result
            make_response("Got it, I'll remember that!"),
        ])

        reply = await chat(_msg("I really like jasmine tea"))

        assert "remember" in reply.text.lower()
        assert "jasmine tea" in read_core()

class TestToolCallReminder:
    """LLM calls manage_reminder tool."""

    @pytest.mark.asyncio
    async def test_create_reminder(self, mock_llm_factory, monkeypatch):
        import mochi.config as config
        monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
        mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("request_tools", {"skills": ["reminder"]}),
            ]),
            make_response(tool_calls=[
                make_tool_call("manage_reminder", {
                    "action": "create",
                    "message": "Take a break",
                    "remind_at": "2099-01-01T12:00:00",
                }),
            ]),
            make_response("Reminder set!"),
        ])

        reply = await chat(_msg("Remind me to take a break"))

        assert "reminder" in reply.text.lower() or "set" in reply.text.lower()
        # Reminder is in the future, so it won't show in get_pending_reminders
        # (which filters remind_at <= now). Verify via direct DB query.
        from mochi.db import _connect
        conn = _connect()
        rows = conn.execute(
            "SELECT message FROM reminders WHERE fired = 0"
        ).fetchall()
        conn.close()
        assert any("Take a break" in r[0] for r in rows)

        executions = get_recent_tool_executions(1)
        assert len(executions) == 1
        assert executions[0]["tool_name"] == "manage_reminder"
        assert executions[0]["arguments"]["message"] == "Take a break"
        assert executions[0]["status"] == "success"
        assert executions[0]["state_changed"] is True

    @pytest.mark.asyncio
    async def test_followup_gets_real_receipt_without_replayed_tool_protocol(
        self, mock_llm_factory, monkeypatch,
    ):
        import mochi.config as config
        monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
        mock = mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("request_tools", {"skills": ["reminder"]}),
            ]),
            make_response(tool_calls=[
                make_tool_call("manage_reminder", {
                    "action": "create",
                    "message": "Submit report",
                    "remind_at": "2099-01-01T12:00:00",
                }),
            ]),
            make_response("Reminder set!"),
            make_response("Okay, I'll change it."),
        ])

        await chat(_msg("Remind me to submit the report"))
        await chat(_msg("把刚才那个改成后天"))

        followup_messages = mock.call_log[3]["messages"]
        system_prompt = followup_messages[0]["content"]
        assert "最近已确认的系统操作" in system_prompt
        assert "Reminder #" in system_prompt
        assert "Submit report" in system_prompt
        assert all(message["role"] != "tool" for message in followup_messages)
        assert all(
            "tool_calls" not in message for message in followup_messages
            if message["role"] == "assistant"
        )


class TestMultiToolLoop:
    """LLM makes multiple sequential tool calls across rounds."""

    @pytest.mark.asyncio
    async def test_parallel_tool_calls(self, mock_llm_factory, monkeypatch):
        """Single LLM response with multiple tool_calls."""
        import mochi.config as config
        monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
        mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("request_tools", {"skills": ["todo"]}),
            ]),
            # The requested tool becomes available only in the next round.
            make_response(tool_calls=[
                make_tool_call("manage_todo", {
                    "action": "add",
                    "task": "Research hiking trails",
                }),
            ]),
            # Final reply after both tool results.
            make_response("Noted your hobby and added a todo!"),
        ])

        reply = await chat(_msg("I like hiking, add research trails to my list"))

        assert "noted" in reply.text.lower() or "todo" in reply.text.lower()
        todos = get_todos(1)
        assert any("hiking" in t["task"].lower() for t in todos)
