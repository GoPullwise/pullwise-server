from __future__ import annotations

import json
import os
import signal
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class RuntimeProcess:
    pid: int
    parent_pid: int
    command_line: str


def _process_records() -> list[RuntimeProcess]:
    if os.name == "nt":
        script = (
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
            "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine | "
            "ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        payload = json.loads(result.stdout)
        rows = payload if isinstance(payload, list) else [payload]
        return [
            RuntimeProcess(int(row["ProcessId"]), int(row["ParentProcessId"]), str(row.get("CommandLine") or ""))
            for row in rows
        ]

    records = []
    for path in Path("/proc").glob("[0-9]*"):
        try:
            status = (path / "status").read_text(encoding="utf-8", errors="replace")
            parent_line = next(line for line in status.splitlines() if line.startswith("PPid:"))
            command = (path / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
            records.append(RuntimeProcess(int(path.name), int(parent_line.split(":", 1)[1]), command))
        except (OSError, StopIteration, ValueError):
            continue
    return records


def _terminate_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _matching_run_pids(runs_root: Path, urls: Mapping[str, str]) -> Iterable[int]:
    seen = set()
    for run_root in sorted(runs_root.glob("*-*"), reverse=True):
        try:
            pid = int(run_root.name.rsplit("-", 1)[1])
            payload = json.loads((run_root / "report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if payload.get("urls") == dict(urls) and pid not in seen:
            seen.add(pid)
            yield pid


def _expected_child(record: RuntimeProcess, urls: Mapping[str, str]) -> bool:
    command = record.command_line.lower().replace("\\", "/")
    ports = {name: urlparse(url).port for name, url in urls.items()}
    if "pullwise_server" in command and f"--port {ports['server']}" in command:
        return True
    if "npm" in command and "run dev" in command and any(
        f"--port {ports[name]}" in command for name in ("web", "admin")
    ):
        return True
    return "src/main.ts watch" in command or "src/main.ts serve" in command


def replace_previous_local_runtime(
    runs_root: Path,
    urls: Mapping[str, str],
    *,
    process_records: Callable[[], Iterable[RuntimeProcess]] = _process_records,
    terminate_tree: Callable[[int], None] = _terminate_tree,
) -> int | None:
    records = list(process_records())
    by_pid = {record.pid: record for record in records}
    for supervisor_pid in _matching_run_pids(runs_root, urls):
        supervisor = by_pid.get(supervisor_pid)
        if supervisor and "local_debug_loop.py" in supervisor.command_line.lower().replace("\\", "/"):
            terminate_tree(supervisor_pid)
            return supervisor_pid
        children = [
            record
            for record in records
            if record.parent_pid == supervisor_pid and _expected_child(record, urls)
        ]
        if children:
            for child in children:
                terminate_tree(child.pid)
            return supervisor_pid
    return None
