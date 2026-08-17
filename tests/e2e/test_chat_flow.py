"""E2E tests for the chat flow: message → LLM → tool dispatch → DB → response."""

import pytest

from mochi.transport import IncomingMessage
from mochi.ai_client import chat
from tests.e2e.mock_llm import make_response, make_tool_call


def _msg(text: str, user_id: int = 1, channel_id: int = 100) -> IncomingMessage:
    """Helper to create an IncomingMessage."""
    return IncomingMessage(
        user_id=user_id, channel_id=channel_id,
        text=text, transport="fake",
    )


class TestSimpleReply:
    @pytest.mark.asyncio
    async def test_main_can_request_bedtime(self, mock_llm_factory, monkeypatch):
        import mochi.heartbeat as heartbeat

        monkeypatch.setattr(heartbeat, "bedtime_tool_available", lambda: True)
        mock = mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("enter_bedtime", {}),
            ]),
            make_response("Good night. I'll get some rest too."),
        ])

        reply = await chat(_msg("I'm heading to bed"))

        assert reply.bedtime_requested is True
        assert reply.text == "Good night. I'll get some rest too."
        assert any(
            tool["function"]["name"] == "enter_bedtime"
            for tool in mock.call_log[0]["tools"]
        )
        assert "Bedtime will begin after this turn" in (
            mock.call_log[1]["messages"][-1]["content"]
        )

    @pytest.mark.asyncio
    async def test_update_core(self, mock_llm_factory):
        from mochi.core_store import read_core, replace_core
        replace_core("Core anchor")
        mock = mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("update_core", {
                    "content": "Core anchor\n\nUser likes jasmine tea",
                }),
            ]),
            # Round 2: LLM gives final reply after tool result
            make_response("Got it, I'll remember that!"),
        ])

        reply = await chat(_msg("I really like jasmine tea"))

        assert "remember" in reply.text.lower()
        assert "jasmine tea" in read_core()
        definition = next(
            tool for tool in mock.call_log[0]["tools"]
            if tool["function"]["name"] == "update_core"
        )
        parameters = definition["function"]["parameters"]
        assert parameters["required"] == ["content"]
        assert set(parameters["properties"]) == {"content"}

    @pytest.mark.asyncio
    async def test_only_first_core_write_in_turn_can_commit(
        self, mock_llm_factory,
    ):
        from mochi.core_store import read_core, replace_core

        replace_core("Core before")
        mock = mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("update_core", {
                    "content": "First complete revision",
                }),
                make_tool_call("update_core", {
                    "content": "Stale second revision",
                }),
            ]),
            make_response("Saved safely."),
        ])

        await chat(_msg("整理 Core"))

        assert read_core() == "First complete revision"
        results = [
            item["content"]
            for item in mock.call_log[1]["messages"]
            if item["role"] == "tool"
        ]
        assert any("No second write was applied" in item for item in results)

    @pytest.mark.asyncio
    async def test_later_round_cannot_replace_core_written_this_turn(
        self, mock_llm_factory,
    ):
        from mochi.core_store import read_core, replace_core

        replace_core("Core before")
        mock = mock_llm_factory([
            make_response(tool_calls=[make_tool_call("update_core", {
                "content": "First complete revision",
            })]),
            make_response(tool_calls=[make_tool_call("update_core", {
                "content": "Stale later revision",
            })]),
            make_response("Done."),
        ])

        await chat(_msg("整理 Core"))

        assert read_core() == "First complete revision"
        later_results = [
            item["content"]
            for item in mock.call_log[2]["messages"]
            if item["role"] == "tool"
        ]
        assert any(
            "No second write was applied" in item for item in later_results
        )

class TestToolCallReminder:
    """LLM calls manage_reminder tool."""

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
