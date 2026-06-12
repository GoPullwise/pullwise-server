from __future__ import annotations

import ctypes
import os
import platform
import shutil
import time
from typing import Any


_PROC_MEMINFO_PATH = "/proc/meminfo"
_PROC_STAT_PATH = "/proc/stat"
_CPU_SAMPLE_INTERVAL_SECONDS = 0.05


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


def _read_proc_cpu_times() -> tuple[int, int] | None:
    try:
        with open(_PROC_STAT_PATH, "r", encoding="utf-8") as stat_file:
            first = stat_file.readline()
    except OSError:
        return None

    parts = first.split()
    if not parts or parts[0] != "cpu":
        return None
    try:
        values = [int(part) for part in parts[1:]]
    except ValueError:
        return None
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return idle, total


def cpu_usage_percent(sample_interval_seconds: float = _CPU_SAMPLE_INTERVAL_SECONDS) -> float | None:
    first = _read_proc_cpu_times()
    if not first:
        return None
    time.sleep(max(0.0, sample_interval_seconds))
    second = _read_proc_cpu_times()
    if not second:
        return None

    idle_delta = second[0] - first[0]
    total_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (1.0 - (idle_delta / total_delta)))), 1)


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
        "usagePercent": cpu_usage_percent(),
        "logicalCount": os.cpu_count(),
        "loadAverage": load_average_payload(),
        "sampleIntervalSeconds": _CPU_SAMPLE_INTERVAL_SECONDS,
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


def server_metrics_payload(*, storage_path: str, timestamp: int | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "collectedAt": int(timestamp if timestamp is not None else time.time()),
        "server": {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "pythonVersion": platform.python_version(),
            "processId": os.getpid(),
        },
        "cpu": cpu_payload(),
        "memory": memory_payload(),
        "storage": storage_payload(storage_path),
    }
