from unittest.mock import AsyncMock

import pytest

from mochi.db import get_skill_config, set_skill_config
from mochi.main import _restore_weixin_owner_state
from mochi.transport.weixin import WeixinTransport


@pytest.mark.asyncio
async def test_restored_context_allows_proactive_send():
    transport = WeixinTransport()
    transport._session = object()
    transport._send_text = AsyncMock(return_value=True)

    transport.restore_owner_id(
        "owner",
        context_token="persisted-context",
        source="test",
    )

    assert await transport.send_message(1, "hello")
    transport._send_text.assert_awaited_once_with(
        "owner",
        "hello",
        "persisted-context",
    )


@pytest.mark.asyncio
async def test_proactive_send_waits_for_owner_context(caplog):
    transport = WeixinTransport()
    transport._session = object()
    transport._send_text = AsyncMock(return_value=True)
    transport.restore_owner_id("owner", source="test")

    assert not await transport.send_message(1, "hello")
    transport._send_text.assert_not_awaited()
    assert "owner context is not ready" in caplog.text


def test_owner_context_is_persisted_without_plaintext(monkeypatch):
    transport = WeixinTransport()
    transport.restore_owner_id("owner", source="test")
    monkeypatch.setattr(
        "mochi.admin.admin_crypto.encrypt_api_key",
        lambda value: f"gAAAAA:{value}",
    )

    transport._remember_context_token("owner", "fresh-context")

    stored = get_skill_config("_transport:wechat")
    assert stored["owner_context_token"] == "gAAAAA:fresh-context"
    assert transport._context_tokens["owner"] == "fresh-context"


def test_owner_context_stays_memory_only_without_encryption(monkeypatch, caplog):
    transport = WeixinTransport()
    transport.restore_owner_id("owner", source="test")
    monkeypatch.setattr(
        "mochi.admin.admin_crypto.encrypt_api_key",
        lambda value: value,
    )

    transport._remember_context_token("owner", "fresh-context")

    assert "owner_context_token" not in get_skill_config("_transport:wechat")
    assert transport._context_tokens["owner"] == "fresh-context"
    assert "remains memory-only" in caplog.text


def test_startup_restores_decrypted_owner_context(monkeypatch):
    set_skill_config("_transport:wechat", "owner_weixin_id", "owner")
    set_skill_config(
        "_transport:wechat",
        "owner_context_token",
        "gAAAAA-encrypted-context",
    )
    monkeypatch.setattr(
        "mochi.admin.admin_crypto.decrypt_api_key",
        lambda value: "decrypted-context",
    )
    transport = WeixinTransport()

    _restore_weixin_owner_state(transport)

    assert transport._owner_weixin_id == "owner"
    assert transport._context_tokens["owner"] == "decrypted-context"


@pytest.mark.asyncio
async def test_non_owner_message_is_rejected_before_main_dispatch(monkeypatch):
    import mochi.transport.weixin as weixin

    transport = WeixinTransport()
    transport.restore_owner_id("owner", source="test")
    callback = AsyncMock()
    monkeypatch.setattr(weixin, "_on_message_callback", callback)

    await transport._handle_message({
        "from_user_id": "intruder",
        "context_token": "intruder-context",
        "item_list": [{
            "type": 1,
            "text_item": {"text": "hello"},
        }],
    })

    callback.assert_not_awaited()
    assert "intruder" not in transport._context_tokens
