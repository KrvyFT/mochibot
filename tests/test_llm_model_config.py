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


def test_admin_accepts_https_compatible_endpoint_and_rejects_unsafe_urls(monkeypatch):
    import mochi.admin.admin_db as admin_db

    monkeypatch.setattr(admin_db, "encrypt_api_key", lambda value: value)
    admin_db.upsert_model(
        "deepseek", "openai", "model", "key", "https://api.deepseek.com/v1",
    )
    admin_db.upsert_model(
        "apiyi", "openai", "claude-model", "key", "https://api.apiyi.com/v1",
    )
    admin_db.set_tier_assignment("main", "apiyi")
    from mochi.db import init_db
    init_db()
    assert admin_db.list_tier_assignments()["main"] == "apiyi"
    for unsafe in (
        "http://api.example.com/v1",
        "https://user:pass@api.example.com/v1",
        "https://api.example.com/v1?token=value",
        "https://api.example.com/v1#fragment",
        "https://api.example.com:bad/v1",
        "https://api.exam\nple.com/v1",
        "https://api.example.com/v1/chat/completions",
    ):
        with pytest.raises(ValueError, match="HTTPS API root"):
            admin_db.upsert_model(
                "unsafe", "openai", "model", "key", unsafe,
            )
    with pytest.raises(ValueError, match="official API"):
        admin_db.upsert_model(
            "anthropic-proxy", "anthropic", "model", "key",
            "https://api.example.com/v1",
        )
