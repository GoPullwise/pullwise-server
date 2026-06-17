from __future__ import annotations

import ctypes
import math
import os
import platform
import shutil
import time
from typing import Any


_PROC_MEMINFO_PATH = "/proc/meminfo"
SERVER_METRICS_HISTORY_STATE_KEY = "server_metrics_history"
SERVER_METRICS_HISTORY_LIMIT = 180
SERVER_METRICS_HISTORY_MIN_INTERVAL_SECONDS = 10
MACHINE_IDENTITY_KEYS = (
    "hostname",
    "platform",
    "system",
    "release",
    "machine",
    "pythonVersion",
    "processId",
)


def _percent(numerator: int | float | None, denominator: int | float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round(max(0.0, min(100.0, (float(numerator) / float(denominator)) * 100.0)), 1)


def _linux_memory_bytes() -> tuple[int | None, int | None]:
    try:
        with open(_PROC_MEMINFO_PATH, "r", encoding="utf-8") as meminfo:
            values: dict[str, int] = {}
            for line in meminfo:
                key, _, rest = line.partition(":")
                amount = rest.strip().split(" ", 1)[0]
                if amount.isdigit():
                    values[key] = int(amount) * 1024
    except OSError:
        return None, None

    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if available is None:
        available = sum(values.get(key, 0) for key in ("MemFree", "Buffers", "Cached"))
    return total, available


def _windows_memory_bytes() -> tuple[int | None, int | None]:
    if platform.system().lower() != "windows":
        return None, None

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None, None
    except (AttributeError, OSError):
        return None, None
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def _sysconf_memory_bytes() -> tuple[int | None, int | None]:
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        page_count = int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return None, None
    total = page_size * page_count if page_size > 0 and page_count > 0 else None
    return total, None


def memory_payload() -> dict[str, Any]:
    total, available = _linux_memory_bytes()
    if total is None:
        total, available = _windows_memory_bytes()
    if total is None:
        total, available = _sysconf_memory_bytes()

    used = total - available if total is not None and available is not None else None
    return {
        "totalBytes": total,
        "availableBytes": available,
        "usedBytes": used,
        "usedPercent": _percent(used, total),
    }


def load_average_payload() -> dict[str, float] | None:
    try:
        one, five, fifteen = os.getloadavg()
    except (AttributeError, OSError):
        return None
    return {
        "oneMinute": round(float(one), 2),
        "fiveMinute": round(float(five), 2),
        "fifteenMinute": round(float(fifteen), 2),
    }


def cpu_payload() -> dict[str, Any]:
    return {
        "logicalCount": os.cpu_count(),
        "loadAverage": load_average_payload(),
    }


def _existing_storage_path(path: str) -> str:
    candidate = os.path.abspath(path or os.getcwd())
    while candidate and not os.path.exists(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    return candidate or os.path.abspath(os.getcwd())


def storage_payload(path: str) -> dict[str, Any]:
    requested_path = os.path.abspath(path or os.getcwd())
    measured_path = _existing_storage_path(requested_path)
    try:
        usage = shutil.disk_usage(measured_path)
    except OSError:
        return {
            "path": requested_path,
            "measuredPath": measured_path,
            "totalBytes": None,
            "usedBytes": None,
            "freeBytes": None,
            "usedPercent": None,
        }

    used = usage.total - usage.free
    return {
        "path": requested_path,
        "measuredPath": measured_path,
        "totalBytes": int(usage.total),
        "usedBytes": int(used),
        "freeBytes": int(usage.free),
        "usedPercent": _percent(used, usage.total),
    }


def machine_identity_payload() -> dict[str, Any]:
    return {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "pythonVersion": platform.python_version(),
        "processId": os.getpid(),
    }


def machine_metrics_payload(*, identity_key: str, storage_path: str, timestamp: int | None = None) -> dict[str, Any]:
    identity = identity_key if identity_key in {"server", "worker"} else "machine"
    return {
        "ok": True,
        "collectedAt": int(timestamp if timestamp is not None else time.time()),
        identity: machine_identity_payload(),
        "cpu": cpu_payload(),
        "memory": memory_payload(),
        "storage": storage_payload(storage_path),
    }


def server_metrics_payload(*, storage_path: str, timestamp: int | None = None) -> dict[str, Any]:
    return machine_metrics_payload(identity_key="server", storage_path=storage_path, timestamp=timestamp)


def _number_or_none(value: object) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def _text_or_none(value: object, *, limit: int = 500) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "").splitlines()[0].strip()
    return text[:limit] if text else None


def _compact_resource_payload(resource: object, keys: tuple[str, ...]) -> dict[str, int | float | None]:
    if not isinstance(resource, dict):
        return {key: None for key in keys}
    return {key: _number_or_none(resource.get(key)) for key in keys}


def _clean_identity_payload(identity: object) -> dict[str, Any]:
    if not isinstance(identity, dict):
        return {}
    clean: dict[str, Any] = {}
    for key in MACHINE_IDENTITY_KEYS:
        value = identity.get(key)
        if key == "processId":
            clean[key] = _number_or_none(value)
        else:
            clean[key] = _text_or_none(value, limit=180)
    return clean


def _clean_storage_payload(storage: object) -> dict[str, Any]:
    clean = _compact_resource_payload(
        storage,
        ("totalBytes", "freeBytes", "usedBytes", "usedPercent"),
    )
    if isinstance(storage, dict):
        clean["path"] = _text_or_none(storage.get("path"), limit=500)
        clean["measuredPath"] = _text_or_none(storage.get("measuredPath"), limit=500)
    else:
        clean["path"] = None
        clean["measuredPath"] = None
    return clean


def sanitize_machine_metrics_payload(
    payload: object,
    *,
    identity_key: str,
    fallback_timestamp: int | None = None,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    clean_identity_key = identity_key if identity_key in {"server", "worker"} else "machine"
    cpu = payload.get("cpu") if isinstance(payload.get("cpu"), dict) else {}
    load_average = cpu.get("loadAverage") if isinstance(cpu.get("loadAverage"), dict) else None
    timestamp = int(_number_or_none(payload.get("collectedAt")) or fallback_timestamp or time.time())
    identity_payload = (
        payload.get(clean_identity_key)
        if isinstance(payload.get(clean_identity_key), dict)
        else payload.get("worker")
        if isinstance(payload.get("worker"), dict)
        else payload.get("server")
    )
    return {
        "ok": payload.get("ok") is not False,
        "collectedAt": timestamp,
        clean_identity_key: _clean_identity_payload(identity_payload),
        "cpu": {
            "logicalCount": _number_or_none(cpu.get("logicalCount")),
            "loadAverage": _compact_resource_payload(
                load_average,
                ("oneMinute", "fiveMinute", "fifteenMinute"),
            )
            if load_average is not None
            else None,
        },
        "memory": _compact_resource_payload(
            payload.get("memory"),
            ("totalBytes", "availableBytes", "usedBytes", "usedPercent"),
        ),
        "storage": _clean_storage_payload(payload.get("storage")),
    }


def machine_metrics_history_sample(payload: dict[str, Any]) -> dict[str, Any]:
    cpu = payload.get("cpu") if isinstance(payload.get("cpu"), dict) else {}
    load_average = cpu.get("loadAverage") if isinstance(cpu.get("loadAverage"), dict) else None
    return {
        "collectedAt": int(_number_or_none(payload.get("collectedAt")) or time.time()),
        "cpu": {
            "logicalCount": _number_or_none(cpu.get("logicalCount")),
            "loadAverage": _compact_resource_payload(
                load_average,
                ("oneMinute", "fiveMinute", "fifteenMinute"),
            )
            if load_average is not None
            else None,
        },
        "memory": _compact_resource_payload(
            payload.get("memory"),
            ("totalBytes", "availableBytes", "usedBytes", "usedPercent"),
        ),
        "storage": _compact_resource_payload(
            payload.get("storage"),
            ("totalBytes", "freeBytes", "usedBytes", "usedPercent"),
        ),
    }


def server_metrics_history_sample(payload: dict[str, Any]) -> dict[str, Any]:
    return machine_metrics_history_sample(payload)


def machine_metrics_history(
    existing_history: object,
    current_payload: dict[str, Any],
    *,
    limit: int = SERVER_METRICS_HISTORY_LIMIT,
    min_interval_seconds: int = SERVER_METRICS_HISTORY_MIN_INTERVAL_SECONDS,
) -> list[dict[str, Any]]:
    history = [item for item in existing_history if isinstance(item, dict)] if isinstance(existing_history, list) else []
    sample = machine_metrics_history_sample(current_payload)
    sample_timestamp = int(sample["collectedAt"])
    clean_history = [
        item
        for item in history
        if isinstance(_number_or_none(item.get("collectedAt")), (int, float))
        and int(item.get("collectedAt")) <= sample_timestamp
    ]

    if clean_history and sample_timestamp - int(clean_history[-1].get("collectedAt", 0)) < max(0, min_interval_seconds):
        clean_history[-1] = sample
    else:
        clean_history.append(sample)

    clean_limit = max(1, min(1000, int(limit or SERVER_METRICS_HISTORY_LIMIT)))
    return clean_history[-clean_limit:]


def server_metrics_history(
    existing_history: object,
    current_payload: dict[str, Any],
    *,
    limit: int = SERVER_METRICS_HISTORY_LIMIT,
    min_interval_seconds: int = SERVER_METRICS_HISTORY_MIN_INTERVAL_SECONDS,
) -> list[dict[str, Any]]:
    return machine_metrics_history(
        existing_history,
        current_payload,
        limit=limit,
        min_interval_seconds=min_interval_seconds,
    )
