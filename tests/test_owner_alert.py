"""LLM failure classification and owner alert delivery."""

import asyncio
import logging

import httpx
import pytest

import mochi.owner_alert as owner_alert
from mochi.llm import describe_error, is_retryable_error


class _StatusError(Exception):
    """Stand-in for a provider SDK error that carries an HTTP status."""

    def __init__(self, status_code: int, message: str = "boom"):
        super().__init__(message)
        self.status_code = status_code


@pytest.fixture(autouse=True)
def clean_alert_state():
    owner_alert.reset()
    yield
    owner_alert.reset()


@pytest.fixture
def alerts(monkeypatch):
    """Capture alert text and force a long cooldown."""
    sent: list[str] = []

    async def sender(text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr("mochi.config.OWNER_ALERT_ENABLED", True)
    monkeypatch.setattr("mochi.config.OWNER_ALERT_COOLDOWN_S", 900)
    owner_alert.set_alert_sender(sender)
    return sent


# ── classification ─────────────────────────────────────────────────────────

def test_transport_failures_are_retryable():
    assert is_retryable_error(httpx.ConnectError("refused"))
    assert is_retryable_error(httpx.ReadTimeout("slow"))
    assert is_retryable_error(TimeoutError("timed out"))
    assert is_retryable_error(ConnectionResetError("reset by peer"))


def test_gateway_backpressure_and_server_errors_are_retryable():
    assert is_retryable_error(_StatusError(429))
    assert is_retryable_error(_StatusError(503))
    assert is_retryable_error(_StatusError(500))


def test_rejected_requests_fail_fast():
    # Retrying a schema or credential problem only delays the report.
    assert not is_retryable_error(_StatusError(400))
    assert not is_retryable_error(_StatusError(401))
    assert not is_retryable_error(_StatusError(404))
    assert not is_retryable_error(ValueError("bad tool schema"))


def test_describe_error_redacts_credentials_and_truncates():
    leaked = describe_error(
        _StatusError(401, "auth failed for Bearer sk-abcdef1234567890 at /v1")
    )
    assert "sk-abcdef1234567890" not in leaked
    assert "[redacted]" in leaked

    token = describe_error(RuntimeError("sent to 8940090209:AAEzmR5ZxyZmq9IWUp5Tc"))
    assert "8940090209:AAEzmR5ZxyZmq9IWUp5Tc" not in token

    long = describe_error(RuntimeError("x" * 900), limit=100)
    assert len(long) <= 101
    assert long.endswith("…")


# ── alert delivery ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_alert_is_sent_once_per_reason_within_cooldown(alerts):
    assert await owner_alert.alert_owner("llm_failure:APIConnectionError", "one")
    assert not await owner_alert.alert_owner("llm_failure:APIConnectionError", "two")
    # A different failure kind is still worth telling the owner about.
    assert await owner_alert.alert_owner("llm_failure:BadRequestError", "three")

    assert alerts == ["one", "three"]


@pytest.mark.asyncio
async def test_alert_without_a_sender_is_a_no_op(monkeypatch):
    monkeypatch.setattr("mochi.config.OWNER_ALERT_ENABLED", True)
    assert not await owner_alert.alert_owner("llm_failure:Whatever", "text")


@pytest.mark.asyncio
async def test_disabled_alerts_do_not_reach_the_transport(monkeypatch, alerts):
    monkeypatch.setattr("mochi.config.OWNER_ALERT_ENABLED", False)
    assert not await owner_alert.alert_owner("llm_failure:APITimeoutError", "text")
    assert alerts == []


@pytest.mark.asyncio
async def test_a_failing_send_never_propagates(caplog):
    async def broken(_text: str) -> bool:
        raise httpx.ConnectError("network is down")

    owner_alert.set_alert_sender(broken)
    with caplog.at_level(logging.WARNING, logger="mochi.owner_alert"):
        assert not await owner_alert.alert_owner("llm_failure:APIConnectionError", "x")

    assert "could not be delivered" in caplog.text


@pytest.mark.asyncio
async def test_a_failed_send_keeps_its_cooldown():
    attempts = 0

    async def broken(_text: str) -> bool:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("network is down")

    owner_alert.set_alert_sender(broken)
    for _ in range(3):
        await owner_alert.alert_owner("llm_failure:APIConnectionError", "x")

    # An alert about connectivity cannot travel over the broken link, so a
    # retry storm would only pile up doomed sends.
    assert attempts == 1


@pytest.mark.asyncio
async def test_a_wedged_transport_does_not_block_forever(monkeypatch):
    monkeypatch.setattr(owner_alert, "_SEND_TIMEOUT_S", 0.05)

    async def hangs(_text: str) -> bool:
        await asyncio.sleep(30)
        return True

    owner_alert.set_alert_sender(hangs)
    assert not await owner_alert.alert_owner("llm_failure:APITimeoutError", "x")
