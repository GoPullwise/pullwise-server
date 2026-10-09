"""Bounded transactional verification email through Cloudflare's EMAIL binding."""
from __future__ import annotations

import asyncio
import re


class EmailDeliveryError(Exception):
    code = "EMAIL_DELIVERY_UNAVAILABLE"

    def __init__(self):
        super().__init__(self.code)


class WorkerEmailGateway:
    """Never log recipients, verification codes or provider error bodies."""

    def __init__(self, env):
        self.binding = getattr(env, "EMAIL", None)
        self.sender = str(getattr(env, "PULLWISE_EMAIL_FROM", ""))
        enabled = str(getattr(env, "PULLWISE_EMAIL_AUTH_ENABLED", "0")) == "1"
        self.configured = bool(enabled and self.binding is not None
            and len(self.sender) <= 254 and re.fullmatch(
                r"[A-Za-z0-9._+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+", self.sender))

    async def send_code(self, email, code, *, expires_in=600):
        if (not self.configured or not isinstance(email, str) or len(email) > 254
                or not email.isascii() or any(char in email for char in "\r\n\x00")
                or not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code)
                or expires_in != 600):
            raise EmailDeliveryError()
        text = (
            f"Your Pullwise verification code is {code}.\n\n"
            "This code expires in 10 minutes. Enter it only in Pullwise. "
            "If you did not request it, you can ignore this email."
        )
        html = (
            '<!doctype html><html lang="en"><body style="margin:0;padding:32px 16px;'
            'background:#f7f7f7;font-family:Arial,sans-serif;color:#171717">'
            '<div style="max-width:480px;margin:auto;padding:32px;'
            'background:#fff;border:1px solid #dedede">'
            '<strong style="font-size:20px">Pullwise</strong>'
            '<p>Your verification code</p>'
            f'<p style="font-family:monospace;font-size:32px;font-weight:bold;'
            f'letter-spacing:6px;color:#5741d9">{code}</p>'
            '<p>This code expires in 10 minutes. Enter it only in Pullwise.</p>'
            '<p style="font-size:13px;color:#626262">If you did not request '
            'this code, ignore this email.</p>'
            '</div></body></html>'
        )
        payload = {"to": email, "from": {"email": self.sender, "name": "Pullwise"},
                   "subject": "Your Pullwise verification code",
                   "text": text, "html": html}
        try:
            import js
            from pyodide.ffi import to_js
        except ModuleNotFoundError:
            # Local reference tests use an in-memory binding, never a provider.
            builder = payload
        else:
            builder = to_js(payload, dict_converter=js.Object.fromEntries)
        try:
            # Unknown delivery is not retried: a timed-out provider may already
            # have accepted the message. A later request is an explicit resend.
            await asyncio.wait_for(self.binding.send(builder), timeout=8)
        except Exception:
            raise EmailDeliveryError() from None
