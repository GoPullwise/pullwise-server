from __future__ import annotations

import json
import logging
import os
from typing import Any


logger = logging.getLogger("pullwise_server.scan")
_DISABLED_VALUES = {"0", "false", "no", "off", "disabled"}


def enabled() -> bool:
    value = os.environ.get("PULLWISE_SCAN_LOGS_ENABLED", "true")
    return value.strip().lower() not in _DISABLED_VALUES


def log_event(event: str, **fields: Any) -> None:
    if not enabled():
        return
    payload = {"event": event}
    payload.update({key: value for key, value in fields.items() if value is not None})
    logger.info(
        "scan_review %s",
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
    )
