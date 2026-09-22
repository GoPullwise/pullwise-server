from __future__ import annotations

import os
import sqlite3
import time
from http import HTTPStatus

from . import db, github_auth
from .product_discovery import GitHubWebhookReceiver
from .product_store import ProductStore


def handle_github_webhook(handler) -> None:
    secret = os.environ.get("PULLWISE_GITHUB_WEBHOOK_SECRET", "")
    app_id = github_auth.app_id()
    if not secret or not app_id:
        return handler.error(HTTPStatus.SERVICE_UNAVAILABLE, "GitHub webhook is not configured.")
    if handler.request_content_length() > 1024 * 1024:
        return handler.error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "GitHub webhook is too large.")
    raw = handler.read_raw_body()
    store = ProductStore(db.database_path())
    try:
        store.initialize()
        GitHubWebhookReceiver(store, app_id=app_id, webhook_secret=secret).receive_github_event(
            raw, signature=handler.headers.get("X-Hub-Signature-256", ""),
            event=handler.headers.get("X-GitHub-Event", ""),
            delivery_id=handler.headers.get("X-GitHub-Delivery", ""), now=int(time.time()),
        )
    except (ValueError, UnicodeError):
        return handler.error(HTTPStatus.BAD_REQUEST, "Invalid GitHub webhook.")
    except sqlite3.Error:
        return handler.error(HTTPStatus.SERVICE_UNAVAILABLE, "GitHub webhook storage unavailable.")
    # Do not disclose matched private scopes or perform GitHub/model I/O in the ACK path.
    return handler.json({"received": True}, HTTPStatus.ACCEPTED)
