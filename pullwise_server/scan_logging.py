from __future__ import annotations

import json
import logging
import math
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
    payload.update({key: json_safe_value(value) for key, value in fields.items() if value is not None})
    logger.info(
        "scan_review %s",
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True, allow_nan=False),
    )


def json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(key): json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [json_safe_value(item) for item in sorted(value, key=repr)]
    return f"<{type(value).__name__}>"
