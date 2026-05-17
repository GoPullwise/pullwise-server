from __future__ import annotations

import os
import ssl
import smtplib
from email.message import EmailMessage


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_flag(name: str, default: str = "false") -> bool:
    return env(name, default).strip().lower() in {"1", "true", "yes", "on"}


def selected_provider() -> str:
    return env("PULLWISE_EMAIL_PROVIDER", "").strip().lower()


def smtp_configured() -> bool:
    provider = selected_provider()
    if provider and provider != "smtp":
        return False
    return bool(env("PULLWISE_SMTP_HOST") and env("PULLWISE_EMAIL_FROM"))


def email_delivery_configured() -> bool:
    return smtp_configured()


def send_magic_link_email(email: str, magic_link: str) -> None:
    if not smtp_configured():
        raise RuntimeError("SMTP email delivery is not configured.")

    message = EmailMessage()
    message["Subject"] = "Sign in to Pullwise"
    message["From"] = env("PULLWISE_EMAIL_FROM")
    message["To"] = email
    message.set_content(
        "\n".join(
            [
                "Use this link to sign in to Pullwise:",
                "",
                magic_link,
                "",
                "This link expires in 15 minutes. If you did not request it, you can ignore this email.",
            ]
        )
    )

    host = env("PULLWISE_SMTP_HOST")
    port = int(env("PULLWISE_SMTP_PORT", "587"))
    timeout = int(env("PULLWISE_SMTP_TIMEOUT_SECONDS", "15"))
    username = env("PULLWISE_SMTP_USERNAME")
    password = env("PULLWISE_SMTP_PASSWORD")

    with smtplib.SMTP(host, port, timeout=timeout) as smtp:
        if env_flag("PULLWISE_SMTP_STARTTLS", "true"):
            smtp.starttls(context=ssl.create_default_context())
        if username or password:
            smtp.login(username, password)
        smtp.send_message(message)
