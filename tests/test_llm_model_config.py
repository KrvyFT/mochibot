from types import SimpleNamespace

import pytest

from mochi import llm


def test_only_official_chat_providers_are_accepted(monkeypatch):
    monkeypatch.setattr(llm, "OpenAIProvider", lambda **kwargs: ("openai", kwargs))
    monkeypatch.setattr(
        llm, "AnthropicProvider", lambda **kwargs: ("anthropic", kwargs),
    )

    assert llm._make_client(
        "openai", "key", "deepseek-chat", "https://api.deepseek.com/v1",
    )[0] == "openai"
    assert llm._make_client(
        "openai",
        "key",
        "gemini-model",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    )[0] == "openai"
    assert llm._make_client("anthropic", "key", "claude", "")[0] == "anthropic"
    for provider in ("azure_openai", "gemini", "deepseek", "custom"):
        with pytest.raises(ValueError):
            llm._make_client(provider, "key", "model", "")


def test_admin_rejects_unofficial_chat_endpoint(monkeypatch):
    import mochi.admin.admin_db as admin_db

    monkeypatch.setattr(admin_db, "encrypt_api_key", lambda value: value)
    admin_db.upsert_model(
        "deepseek", "openai", "model", "key", "https://api.deepseek.com/v1",
    )
    with pytest.raises(ValueError, match="official"):
        admin_db.upsert_model(
            "custom", "openai", "model", "key", "https://example.invalid/v1",
        )


def test_openai_compatible_chat_handles_text_and_tools():
    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="weather", arguments='{"city":"Tokyo"}'),
    )
    responses = [
        SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="hello", tool_calls=[]),
                finish_reason="stop",
            )],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[tool_call]),
                finish_reason="stop",
            )],
            usage=None,
        ),
    ]

    class Completions:
        def create(self, **kwargs):
            return responses.pop(0)

    provider = llm.OpenAIProvider.__new__(llm.OpenAIProvider)
    provider._model = "model"
    provider._base_url = "https://api.deepseek.com/v1"
    provider._use_max_completion_tokens = None
    provider._use_temperature = None
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions()),
    )

    assert provider.chat([{"role": "user", "content": "hi"}]).content == "hello"
    result = provider.chat(
        [{"role": "user", "content": "weather"}],
        tools=[{"type": "function", "function": {"name": "weather"}}],
    )
    assert result.tool_calls == [{
        "id": "call-1",
        "name": "weather",
        "arguments": {"city": "Tokyo"},
    }]
