"""Provider protocol translation contract."""

from types import SimpleNamespace

from mochi import llm


def test_openai_response_preserves_reasoning_content_for_tool_followup():
    message = SimpleNamespace(
        content="",
        reasoning_content="I should check the weather first.",
    )
    choice = SimpleNamespace(message=message, finish_reason="tool_calls")

    response = llm._openai_response(
        choice,
        usage=None,
        model="deepseek-reasoner",
        tool_calls=[{
            "id": "call_weather",
            "name": "get_weather",
            "arguments": {"city": "Suzhou"},
        }],
    )

    assert response.reasoning_content == (
        "I should check the weather first."
    )
