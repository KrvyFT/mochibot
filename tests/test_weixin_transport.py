import time
from unittest.mock import AsyncMock

import pytest

from mochi.ai_client import ChatResult
from mochi.db import get_skill_config, set_skill_config
from mochi.main import _restore_weixin_owner_state
from mochi.transport.weixin import (
    BASE_INFO,
    ILINK_APP_CLIENT_VERSION,
    WeixinTransport,
    _build_headers,
)


@pytest.mark.asyncio
async def test_protocol_restore_and_missing_context_boundary(caplog):
    headers = _build_headers()
    assert headers["iLink-App-Id"] == "bot"
    assert headers["iLink-App-ClientVersion"] == str(ILINK_APP_CLIENT_VERSION)
    assert BASE_INFO["channel_version"] == "2.4.6"

    transport = WeixinTransport()
    transport._session = object()
    transport._send_text = AsyncMock(return_value=True)
    transport.restore_owner_id(
        "owner", context_token="persisted-context", source="test",
    )
    transport._context_token_at["owner"] = time.time()
    assert await transport.send_message(1, "hello")
    transport._send_text.assert_awaited_once_with(
        "owner", "hello", "persisted-context",
    )

    missing = WeixinTransport()
    missing._session = object()
    missing._send_text = AsyncMock(return_value=True)
    missing.restore_owner_id("owner", source="test")
    assert not await missing.send_message(1, "hello")
    missing._send_text.assert_not_awaited()
    assert "owner context is not ready" in caplog.text


def test_context_persistence_is_encrypted_and_restart_safe(monkeypatch, caplog):
    transport = WeixinTransport()
    transport.restore_owner_id("owner", source="test")
    monkeypatch.setattr(
        "mochi.admin.admin_crypto.encrypt_api_key",
        lambda value: f"gAAAAA:{value}",
    )
    transport._remember_context_token("owner", "fresh-context")
    assert get_skill_config("_transport:wechat")["owner_context_token"] == (
        "gAAAAA:fresh-context"
    )

    set_skill_config(
        "_transport:wechat", "owner_context_token", "gAAAAA-encrypted-context",
    )
    set_skill_config("_transport:wechat", "owner_weixin_id", "owner")
    monkeypatch.setattr(
        "mochi.admin.admin_crypto.decrypt_api_key",
        lambda value: "decrypted-context",
    )
    restored = WeixinTransport()
    _restore_weixin_owner_state(restored)
    assert restored._context_tokens["owner"] == "decrypted-context"

    monkeypatch.setattr(
        "mochi.admin.admin_crypto.encrypt_api_key", lambda value: value,
    )
    memory_only = WeixinTransport()
    memory_only.restore_owner_id("other-owner", source="test")
    memory_only._remember_context_token("other-owner", "memory-context")
    assert memory_only._context_tokens["other-owner"] == "memory-context"
    assert "remains memory-only" in caplog.text


@pytest.mark.asyncio
async def test_stale_context_refresh_covers_chat_and_preserves_newer_inbound(
    monkeypatch,
):
    monkeypatch.setattr(
        "mochi.admin.admin_crypto.encrypt_api_key",
        lambda value: f"gAAAAA:{value}",
    )
    transport = WeixinTransport()
    transport._session = object()
    transport._weixin_send_message = AsyncMock(return_value={
        "ret": 0, "errcode": 0,
    })
    transport._weixin_get_config = AsyncMock(return_value={
        "ret": 0, "errcode": 0, "context_token": "refreshed-context",
    })
    transport.restore_owner_id(
        "owner", context_token="stale-context", source="test",
    )
    assert await transport.send_chat_result(1, ChatResult(text="hello"))
    transport._weixin_send_message.assert_awaited_once_with(
        "owner", "hello", "refreshed-context",
    )
    assert get_skill_config("_transport:wechat")["owner_context_token"] == (
        "gAAAAA:refreshed-context"
    )

    transport.restore_owner_id(
        "owner", context_token="stale-again", source="test",
    )

    async def refresh(_user_id, _context_token):
        transport._remember_context_token("owner", "newer-inbound-context")
        return {
            "ret": 0,
            "errcode": 0,
            "context_token": "refreshed-stale-context",
        }

    transport._weixin_get_config = AsyncMock(side_effect=refresh)
    assert await transport.send_message(1, "second")
    assert transport._context_tokens["owner"] == "newer-inbound-context"


@pytest.mark.asyncio
async def test_context_rejection_waits_for_fresh_inbound(monkeypatch, caplog):
    monkeypatch.setattr(
        "mochi.admin.admin_crypto.encrypt_api_key",
        lambda value: f"gAAAAA:{value}",
    )
    transport = WeixinTransport()
    transport._session = object()
    transport.restore_owner_id(
        "owner", context_token="exhausted-context", source="test",
    )
    set_skill_config(
        "_transport:wechat",
        "owner_context_token",
        "gAAAAA:exhausted-context",
    )
    transport._context_token_at["owner"] = time.time()
    transport._weixin_send_message = AsyncMock(return_value={
        "ret": -2, "errcode": 0,
    })

    assert not await transport.send_message(1, "hello")
    assert transport._context_tokens["owner"] == "exhausted-context"
    assert transport._context_token_at["owner"] == 0
    assert get_skill_config("_transport:wechat")["owner_context_token"] == (
        "gAAAAA:exhausted-context"
    )
    assert "preserving context" in caplog.text

    transport._weixin_get_config = AsyncMock(return_value={
        "ret": -2, "errcode": 0,
    })
    transport._weixin_send_message.reset_mock()
    assert not await transport.send_message(1, "retry after refresh")
    transport._weixin_send_message.assert_not_awaited()
    assert "owner" not in transport._context_tokens
    assert get_skill_config("_transport:wechat")["owner_context_token"] == ""

    transport._remember_context_token("owner", "fresh-inbound-context")
    transport._weixin_send_message = AsyncMock(return_value={
        "ret": 0, "errcode": 0,
    })
    assert await transport.send_message(1, "welcome back")


@pytest.mark.asyncio
async def test_refresh_rejection_does_not_attempt_send():
    transport = WeixinTransport()
    transport._session = object()
    transport.restore_owner_id(
        "owner", context_token="expired-context", source="test",
    )
    transport._weixin_get_config = AsyncMock(return_value={
        "ret": -2, "errcode": 0,
    })
    transport._weixin_send_message = AsyncMock()

    assert not await transport.send_message(1, "hello")
    transport._weixin_send_message.assert_not_awaited()
    assert "owner" not in transport._context_tokens


@pytest.mark.asyncio
async def test_send_restriction_can_recover_after_context_refresh():
    transport = WeixinTransport()
    transport._session = object()
    transport.restore_owner_id(
        "owner", context_token="rate-limited-context", source="test",
    )
    transport._context_token_at["owner"] = time.time()
    transport._weixin_send_message = AsyncMock(side_effect=[
        {"ret": -2, "errcode": 0},
        {"ret": 0, "errcode": 0},
    ])
    transport._weixin_get_config = AsyncMock(return_value={
        "ret": 0,
        "errcode": 0,
        "context_token": "rate-limited-context",
    })

    assert not await transport.send_message(1, "first")
    assert transport._context_tokens["owner"] == "rate-limited-context"

    assert await transport.send_message(1, "second")
    transport._weixin_get_config.assert_awaited_once_with(
        "owner", "rate-limited-context",
    )


@pytest.mark.asyncio
async def test_send_prefers_newer_inbound_context_during_refresh():
    transport = WeixinTransport()
    transport._session = object()
    transport.restore_owner_id(
        "owner", context_token="stale-context", source="test",
    )

    async def refresh(_user_id, _context_token):
        transport._remember_context_token("owner", "newer-inbound-context")
        return {"ret": -2, "errcode": 0}

    transport._weixin_get_config = AsyncMock(side_effect=refresh)
    transport._weixin_send_message = AsyncMock(return_value={
        "ret": 0, "errcode": 0,
    })

    assert await transport.send_message(1, "hello")
    transport._weixin_send_message.assert_awaited_once_with(
        "owner", "hello", "newer-inbound-context",
    )


@pytest.mark.asyncio
async def test_non_owner_is_rejected_before_main_dispatch(monkeypatch):
    import mochi.transport.weixin as weixin

    transport = WeixinTransport()
    transport.restore_owner_id("owner", source="test")
    callback = AsyncMock()
    monkeypatch.setattr(weixin, "_on_message_callback", callback)
    await transport._handle_message({
        "from_user_id": "intruder",
        "context_token": "intruder-context",
        "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
    })
    callback.assert_not_awaited()
    assert "intruder" not in transport._context_tokens
