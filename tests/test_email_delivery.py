from __future__ import annotations

import os
import unittest
from email.message import EmailMessage
from unittest.mock import Mock, patch

from pullwise_server import email_delivery


class EmailDeliveryTest(unittest.TestCase):
    def test_smtp_is_not_configured_without_real_delivery_settings(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(email_delivery.smtp_configured())
            self.assertFalse(email_delivery.email_delivery_configured())

    def test_smtp_magic_link_email_uses_configured_transport(self) -> None:
        smtp = Mock()
        smtp_class = Mock(return_value=smtp)
        smtp.__enter__ = Mock(return_value=smtp)
        smtp.__exit__ = Mock(return_value=None)
        tls_context = object()

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_EMAIL_PROVIDER": "smtp",
                    "PULLWISE_SMTP_HOST": "smtp.example.com",
                    "PULLWISE_SMTP_PORT": "587",
                    "PULLWISE_SMTP_USERNAME": "apikey",
                    "PULLWISE_SMTP_PASSWORD": "secret",
                    "PULLWISE_SMTP_STARTTLS": "true",
                    "PULLWISE_EMAIL_FROM": "Pullwise <login@pullwise.dev>",
                },
                clear=True,
            ),
            patch("pullwise_server.email_delivery.smtplib.SMTP", smtp_class),
            patch("pullwise_server.email_delivery.ssl.create_default_context", return_value=tls_context) as create_context,
        ):
            email_delivery.send_magic_link_email(
                "dev@example.com",
                "https://api.pullwise.dev/auth/email/callback?token=abc",
            )

        smtp_class.assert_called_once_with("smtp.example.com", 587, timeout=15)
        create_context.assert_called_once()
        smtp.starttls.assert_called_once_with(context=tls_context)
        smtp.login.assert_called_once_with("apikey", "secret")
        sent_message = smtp.send_message.call_args.args[0]
        self.assertIsInstance(sent_message, EmailMessage)
        self.assertEqual(sent_message["To"], "dev@example.com")
        self.assertEqual(sent_message["From"], "Pullwise <login@pullwise.dev>")
        self.assertIn("Sign in to Pullwise", sent_message["Subject"])
        self.assertIn("https://api.pullwise.dev/auth/email/callback?token=abc", sent_message.get_content())


if __name__ == "__main__":
    unittest.main()
