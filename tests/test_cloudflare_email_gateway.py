import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pullwise_server.cloudflare_email_gateway import EmailDeliveryError, WorkerEmailGateway


def gateway(**changes):
    values = {"EMAIL": SimpleNamespace(send=AsyncMock(return_value={"messageId": "local"})),
              "PULLWISE_EMAIL_FROM": "login@auth.pull-wise.com",
              "PULLWISE_EMAIL_AUTH_ENABLED": "1"}
    values.update(changes)
    return WorkerEmailGateway(SimpleNamespace(**values))


def test_sends_structured_plain_text_and_html_without_recipient_reflection():
    mail = gateway()
    asyncio.run(mail.send_code("user+local@example.com", "001234"))
    body = mail.binding.send.await_args.args[0]
    assert body["to"] == "user+local@example.com"
    assert body["from"] == {"email": "login@auth.pull-wise.com", "name": "Pullwise"}
    assert body["subject"] == "Your Pullwise verification code"
    assert all(body[field].isascii() for field in ("subject", "text", "html"))
    assert "001234" in body["text"] and "001234" in body["html"]
    assert "001234" not in body["subject"]
    for field in ("text", "html"):
        assert "10 minutes" in body[field]
        assert "Enter it only in Pullwise." in body[field]
        assert "ignore this email." in body[field]
        assert "user+local@example.com" not in body[field]
    assert '<html lang="en">' in body["html"]
    assert "http" not in body["html"]
    mail.binding.send.assert_awaited_once()


@pytest.mark.parametrize("changes", [
    {"EMAIL": None}, {"PULLWISE_EMAIL_AUTH_ENABLED": "0"},
    {"PULLWISE_EMAIL_FROM": ""}, {"PULLWISE_EMAIL_FROM": "sender@example.com\r\nBcc: victim"},
])
def test_missing_or_invalid_configuration_fails_closed(changes):
    mail = gateway(**changes)
    assert not mail.configured
    with pytest.raises(EmailDeliveryError, match="^EMAIL_DELIVERY_UNAVAILABLE$"):
        asyncio.run(mail.send_code("user@example.com", "001234"))


@pytest.mark.parametrize("code", ["12345", "1234567", "１２３４５６", "1<script>", 123456])
def test_rejects_unsafe_or_nonexact_code_without_sending(code):
    mail = gateway()
    with pytest.raises(EmailDeliveryError):
        asyncio.run(mail.send_code("user@example.com", code))
    mail.binding.send.assert_not_awaited()


def test_provider_failure_is_redacted_and_not_retried():
    mail = gateway()
    mail.binding.send.side_effect = RuntimeError("provider recipient and code should stay private")
    with pytest.raises(EmailDeliveryError) as error:
        asyncio.run(mail.send_code("user@example.com", "001234"))
    assert str(error.value) == "EMAIL_DELIVERY_UNAVAILABLE"
    assert error.value.__cause__ is None
    mail.binding.send.assert_awaited_once()
