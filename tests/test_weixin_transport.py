from unittest.mock import AsyncMock
import time

import pytest

from mochi.db import get_skill_config, set_skill_config
from mochi.main import _restore_weixin_owner_state
from mochi.transport.weixin import (
    BASE_INFO,
    ILINK_APP_CLIENT_VERSION,
    WeixinTransport,
    _build_headers,
)


def test_weixin_requests_identify_current_ilink_protocol():
    headers = _build_headers()

    assert headers["iLink-App-Id"] == "bot"
    assert headers["iLink-App-ClientVersion"] == str(ILINK_APP_CLIENT_VERSION)
    assert BASE_INFO["channel_version"] == "2.4.6"
    assert BASE_INFO["bot_agent"].startswith("mochibot/")


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
    transport._context_token_at["owner"] = time.time()

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
async def test_stale_owner_context_is_refreshed_and_persisted(monkeypatch):
    transport = WeixinTransport()
    transport._session = object()
    transport._weixin_send_message = AsyncMock(return_value={
        "ret": 0,
        "errcode": 0,
    })
    transport._weixin_get_config = AsyncMock(return_value={
        "ret": 0,
        "errcode": 0,
        "context_token": "refreshed-context",
    })
    transport.restore_owner_id(
        "owner",
        context_token="stale-context",
        source="test",
    )
    monkeypatch.setattr(
        "mochi.admin.admin_crypto.encrypt_api_key",
        lambda value: f"gAAAAA:{value}",
    )

    assert await transport.send_message(1, "hello")

    transport._weixin_get_config.assert_awaited_once_with(
        "owner",
        "stale-context",
    )
    transport._weixin_send_message.assert_awaited_once_with(
        "owner",
        "hello",
        "refreshed-context",
    )
    stored = get_skill_config("_transport:wechat")
    assert stored["owner_context_token"] == "gAAAAA:refreshed-context"


@pytest.mark.asyncio
async def test_chat_result_refreshes_stale_context(monkeypatch):
    from mochi.ai_client import ChatResult

    transport = WeixinTransport()
    transport._session = object()
    transport._weixin_get_config = AsyncMock(return_value={
        "ret": 0,
        "errcode": 0,
        "context_token": "refreshed-context",
    })
    transport._weixin_send_message = AsyncMock(return_value={
        "ret": 0,
        "errcode": 0,
    })
    transport.restore_owner_id(
        "owner",
        context_token="stale-context",
        source="test",
    )
    monkeypatch.setattr(
        "mochi.admin.admin_crypto.encrypt_api_key",
        lambda value: f"gAAAAA:{value}",
    )

    assert await transport.send_chat_result(
        1,
        ChatResult(text="hello"),
    )
    transport._weixin_send_message.assert_awaited_once_with(
        "owner",
        "hello",
        "refreshed-context",
    )


@pytest.mark.asyncio
async def test_context_refresh_does_not_overwrite_newer_inbound_token(monkeypatch):
    transport = WeixinTransport()
    transport._session = object()
    transport._weixin_send_message = AsyncMock(return_value={
        "ret": 0,
        "errcode": 0,
    })
    transport.restore_owner_id(
        "owner",
        context_token="stale-context",
        source="test",
    )
    monkeypatch.setattr(
        "mochi.admin.admin_crypto.encrypt_api_key",
        lambda value: f"gAAAAA:{value}",
    )

    async def refresh(_user_id, _context_token):
        transport._remember_context_token("owner", "newer-inbound-context")
        return {
            "ret": 0,
            "errcode": 0,
            "context_token": "refreshed-stale-context",
        }

    transport._weixin_get_config = AsyncMock(side_effect=refresh)

    assert await transport.send_message(1, "hello")
    transport._weixin_send_message.assert_awaited_once_with(
        "owner",
        "hello",
        "newer-inbound-context",
    )
    assert transport._context_tokens["owner"] == "newer-inbound-context"


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
