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

    usage = SimpleNamespace(
        prompt_tokens=453,
        completion_tokens=23,
        total_tokens=476,
        completion_tokens_details=None,
        prompt_tokens_details=None,
        prompt_cache_hit_tokens=384,
    )
    deepseek_usage = llm._openai_response(
        choice,
        usage=usage,
        model="deepseek-v4-flash",
        tool_calls=[],
    )
    assert deepseek_usage.cached_prompt_tokens == 384

    usage.prompt_tokens_details = SimpleNamespace(cached_tokens=256)
    standard_usage = llm._openai_response(
        choice,
        usage=usage,
        model="deepseek-v4-flash",
        tool_calls=[],
    )
    assert standard_usage.cached_prompt_tokens == 256
