from __future__ import annotations

import os
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app


class HandlerHarness:
    headers = {"Host": "api.pullwise.dev"}

    def __init__(self) -> None:
        self.payload = None
        self.status = None

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers_out = headers or {}

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)


class MagicLinkContractsTest(unittest.TestCase):
    def setUp(self) -> None:
        app.MAGIC_LINKS = {}
        app.STATE_LOADED = True
        app.STATE_DIRTY = False

    def test_smtp_magic_link_sends_email_without_returning_token(self) -> None:
        handler = HandlerHarness()
        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_EMAIL_PROVIDER": "smtp",
                    "PULLWISE_SMTP_HOST": "smtp.example.com",
                    "PULLWISE_EMAIL_FROM": "Pullwise <login@pullwise.dev>",
                    "PULLWISE_API_BASE_URL": "https://api.pullwise.dev",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.email_delivery.send_magic_link_email") as send_email,
        ):
            app.PullwiseHandler.handle_magic_link(
                handler,
                {
                    "email": "Dev@Example.com",
                    "redirectTo": "https://app.pullwise.dev/?screen=dashboard",
                },
            )

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["email"], "dev@example.com")
        self.assertTrue(handler.payload["ok"])
        self.assertTrue(handler.payload["sent"])
        self.assertNotIn("magicLink", handler.payload)
        self.assertNotIn("devMagicLink", handler.payload)
        send_email.assert_called_once()
        sent_to, sent_link = send_email.call_args.args
        self.assertEqual(sent_to, "dev@example.com")
        self.assertTrue(sent_link.startswith("https://api.pullwise.dev/auth/email/callback?token="))

    def test_magic_link_uses_forwarded_proxy_base_url(self) -> None:
        handler = HandlerHarness()
        handler.headers = {
            "Host": "api.internal",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "app.pullwise.dev",
            "X-Forwarded-Prefix": "/api",
        }

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_EMAIL_PROVIDER": "smtp",
                    "PULLWISE_SMTP_HOST": "smtp.example.com",
                    "PULLWISE_EMAIL_FROM": "Pullwise <login@pullwise.dev>",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                    "PULLWISE_TRUST_PROXY_HEADERS": "true",
                },
                clear=True,
            ),
            patch("pullwise_server.email_delivery.send_magic_link_email") as send_email,
        ):
            app.PullwiseHandler.handle_magic_link(
                handler,
                {
                    "email": "dev@example.com",
                    "redirectTo": "https://app.pullwise.dev/?screen=dashboard",
                },
            )

        sent_link = send_email.call_args.args[1]
        self.assertTrue(sent_link.startswith("https://app.pullwise.dev/api/auth/email/callback?token="))


if __name__ == "__main__":
    unittest.main()
