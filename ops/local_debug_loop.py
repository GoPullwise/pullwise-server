#!/usr/bin/env python3
"""Run an agent-friendly local Pullwise four-project debug cycle."""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from local_debug_browser import local_browser_entry_urls, open_local_browser_entries
from local_debug_runtime import RuntimeProcess, replace_previous_local_runtime
@dataclass(frozen=True)
class RuntimeConfig:
    workspace: Path
    run_root: Path
    server_port: int = 8080
    web_port: int = 5173
    admin_port: int = 5174
    admin_email: str = "local-admin@pullwise.test"
    profile_root: Path | None = None

    @property
    def server_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    @property
    def web_url(self) -> str:
        return f"http://127.0.0.1:{self.web_port}"

    @property
    def admin_url(self) -> str:
        return f"http://127.0.0.1:{self.admin_port}"


@dataclass(frozen=True)
class ProcessSpec:
    name: str
    cwd: Path
    argv: tuple[str, ...]
    env: dict[str, str]


class Api(Protocol):
    def visit(self, path: str) -> None: ...
    def request(self, method: str, path: str, body: object = None) -> dict: ...

def initial_process_specs(config: RuntimeConfig, python: str, npm: str) -> list[ProcessSpec]:
    origins = f"{config.web_url},{config.admin_url}"
    server_env = {
        "PULLWISE_ENABLE_LOCAL_GITHUB_MOCKS": "true",
        "PULLWISE_MODE": "local",
        "PULLWISE_HOST": "127.0.0.1",
        "PULLWISE_PORT": str(config.server_port),
        "PULLWISE_APP_URL": config.web_url,
        "PULLWISE_ADMIN_APP_URL": config.admin_url,
        "PULLWISE_ALLOWED_ORIGINS": origins,
        "PULLWISE_API_BASE_URL": config.server_url,
        "PULLWISE_WORKER_SERVER_URL": config.server_url,
        "PULLWISE_ADMIN_EMAILS": config.admin_email,
        "PULLWISE_DEV_EMAIL": config.admin_email,
        "PULLWISE_COOKIE_SECURE": "false",
        "PULLWISE_DB_PATH": str(config.run_root / "pullwise.sqlite3"),
        "PULLWISE_LOG_DIR": str(config.run_root / "server-logs"),
    }
    vite_env = {"VITE_API_BASE_URL": config.server_url}
    return [
        ProcessSpec(
            "server",
            config.workspace / "pullwise-server",
            (python, "-m", "pullwise_server", "--host", "127.0.0.1", "--port", str(config.server_port)),
            server_env,
        ),
        ProcessSpec(
            "web",
            config.workspace / "pullwise-web",
            (npm, "run", "dev", "--", "--host", "127.0.0.1", "--port", str(config.web_port), "--strictPort"),
            {**vite_env, "VITE_APP_URL": config.web_url},
        ),
        ProcessSpec(
            "admin",
            config.workspace / "pullwise-admin",
            (npm, "run", "dev", "--", "--host", "127.0.0.1", "--port", str(config.admin_port), "--strictPort"),
            {**vite_env, "VITE_APP_URL": config.admin_url},
        ),
    ]


def worker_process_specs(config: RuntimeConfig, node: str, worker_id: str, token: str) -> list[ProcessSpec]:
    profile_root = config.profile_root or config.run_root / "worker-profiles"
    worker_env = {
        "PULLWISE_SERVER_URL": config.server_url,
        "PULLWISE_WORKER_ID": worker_id,
        "PULLWISE_WORKER_TOKEN": token,
        "PULLWISE_WORKER_VERSION": "0.10.24",
        "PULLWISE_PI_PROFILE_ROOT": str(profile_root),
        "PULLWISE_WORKER_STATE_ROOT": str(config.run_root / "worker-state"),
        "PULLWISE_CHECKOUT_ROOT": str(config.run_root / "worker-checkouts"),
    }
    worker_root = config.workspace / "pullwise-worker"
    return [
        ProcessSpec("worker-watcher", worker_root, (node, "src/main.ts", "watch"), worker_env),
        ProcessSpec("worker-service", worker_root, (node, "src/main.ts", "serve"), worker_env),
    ]


def public_runtime_state(specs: list[ProcessSpec], worker_id: str) -> dict:
    return {
        "workerId": worker_id,
        "processes": [{"name": spec.name, "cwd": str(spec.cwd), "argv": list(spec.argv)} for spec in specs],
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class ApiSession:
    def __init__(self, base_url: str) -> None:
        jar = http.cookiejar.CookieJar()
        self.base_url = base_url.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _NoRedirect())

    def _url(self, path: str) -> str:
        return path if path.startswith(("http://", "https://")) else f"{self.base_url}{path}"

    def visit(self, path: str) -> None:
        request = urllib.request.Request(self._url(path), headers={"User-Agent": "pullwise-local-debug/1"})
        try:
            with self.opener.open(request, timeout=10):
                return
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                return
            raise RuntimeError(f"GET {path} failed ({exc.code})") from exc

    def request(self, method: str, path: str, body: object = None) -> dict:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {"User-Agent": "pullwise-local-debug/1"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self._url(path), data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed ({exc.code}): {raw[:500]}") from exc
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            raise RuntimeError(f"{method} {path} returned a non-object JSON payload")
        return payload


def run_core_smoke(api: Api, web_url: str, worker_id: str) -> dict:
    redirect = urllib.parse.quote(f"{web_url}/dashboard", safe="")
    api.visit(f"/auth/github/callback?redirectTo={redirect}")
    auth = api.request("GET", "/auth/session")
    if not auth.get("authenticated") or not auth.get("admin"):
        raise RuntimeError("local mock session is not authenticated as an administrator")
    repositories_redirect = urllib.parse.quote(f"{web_url}/repositories", safe="")
    authorization = api.request(
        "GET", f"/integrations/github/authorize?scope=all&redirectTo={repositories_redirect}"
    )
    authorization_url = str(authorization.get("url") or "")
    if not authorization_url:
        raise RuntimeError("local repository authorization did not return a callback URL")
    api.visit(authorization_url)
    api.request("POST", "/repositories/sync", {})
    repositories = api.request("GET", "/repositories")
    items = repositories.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise RuntimeError("local repository sync returned no repositories")
    repository = items[0]
    repo_id = str(repository.get("repoId") or repository.get("id") or "")
    if not repo_id:
        raise RuntimeError("local repository has no stable id")
    scan_request = {
        "repoId": repo_id,
        "branch": str(repository.get("defaultBranch") or "main"),
        "requestId": f"local-debug-{uuid.uuid4().hex}",
    }
    preflight = api.request("POST", "/scans/preflight", scan_request)
    if int(preflight.get("allowedCount") or 0) < 1:
        raise RuntimeError("local scan preflight did not allow the selected repository")
    created = api.request("POST", "/scans", scan_request)
    scan_id = str(created.get("id") or "")
    if not scan_id:
        raise RuntimeError("local scan creation returned no scan id")
    api.request("POST", f"/scans/{scan_id}/cancel", {})
    scan = api.request("GET", f"/scans/{scan_id}")
    if scan.get("status") not in {"cancelled", "cancel_requested", "cancelling"}:
        raise RuntimeError(f"local scan did not enter cancellation: {scan.get('status')}")
    workers_payload = api.request("GET", "/admin/workers")
    workers = workers_payload.get("items") or workers_payload.get("workers") or []
    worker = next((item for item in workers if item.get("worker_id") == worker_id), None)
    if not worker:
        raise RuntimeError("local Worker did not register in the Admin control plane")
    api.request("GET", "/admin/status")
    ready = bool(worker.get("readyProviders")) and worker.get("doctor_status") == "ok" and bool(worker.get("runtimeSelection"))
    return {
        "status": "pass",
        "scope": "local-plumbing",
        "userFlow": "authenticated_repository_sync_scan_create_cancel",
        "adminFlow": "authenticated_worker_and_system_status",
        "repository": {"id": repo_id, "fullName": repository.get("fullName")},
        "scan": {"id": scan_id, "status": scan.get("status")},
        "worker": {
            "id": worker_id,
            "status": worker.get("status"),
            "doctorStatus": worker.get("doctor_status"),
            "fullReviewReady": ready,
        },
        "warnings": [] if ready else [
            "Worker has no ready Pi credential/model profile; full AI review completion was not exercised."
        ],
    }


def assert_workspace(config: RuntimeConfig) -> None:
    missing = [name for name in ("pullwise-server", "pullwise-worker", "pullwise-admin", "pullwise-web")
               if not (config.workspace / name).is_dir()]
    if missing:
        raise RuntimeError(f"workspace is missing projects: {', '.join(missing)}")


def assert_ports_free(config: RuntimeConfig) -> None:
    for port in (config.server_port, config.web_port, config.admin_port):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(f"local debug port {port} is already in use") from exc


def wait_url(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {url}")


def spawn(spec: ProcessSpec, run_root: Path) -> subprocess.Popen:
    stdout = (run_root / f"{spec.name}.stdout.log").open("ab")
    stderr = (run_root / f"{spec.name}.stderr.log").open("ab")
    kwargs = {"cwd": spec.cwd, "env": {**os.environ, **spec.env}, "stdout": stdout, "stderr": stderr}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(spec.argv, **kwargs)
    finally:
        stdout.close()
        stderr.close()


def stop_processes(processes: list[subprocess.Popen]) -> None:
    for process in reversed(processes):
        if process.poll() is not None:
            continue
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def write_report(path: Path, report: dict) -> None:
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def executable(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise RuntimeError(f"required executable is unavailable: {name}")
    return resolved


def run(args: argparse.Namespace) -> int:
    server_root = Path(__file__).resolve().parents[1]
    workspace = server_root.parent
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = server_root / ".pullwise" / "local-debug" / "runs" / f"{stamp}-{os.getpid()}"
    profile_root = Path(args.profile_root).resolve() if args.profile_root else run_root / "worker-profiles"
    config = RuntimeConfig(workspace, run_root, args.server_port, args.web_port, args.admin_port, args.admin_email, profile_root)
    entry_urls = local_browser_entry_urls(config)
    replace_previous_local_runtime(server_root / ".pullwise" / "local-debug" / "runs", {"server": config.server_url, "web": config.web_url, "admin": config.admin_url})
    assert_workspace(config)
    assert_ports_free(config)
    run_root.mkdir(parents=True)
    profile_root.mkdir(parents=True, exist_ok=True)
    (run_root / "server-logs").mkdir()
    python = str(server_root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    if not Path(python).is_file():
        python = sys.executable
    npm = executable("npm.cmd" if os.name == "nt" else "npm")
    node = executable("node")
    processes: list[subprocess.Popen] = []
    specs = initial_process_specs(config, python, npm)
    report_path = run_root / "report.json"
    try:
        for spec in specs:
            processes.append(spawn(spec, run_root))
        wait_url(f"{config.server_url}/health", args.startup_timeout)
        wait_url(config.web_url, args.startup_timeout)
        wait_url(config.admin_url, args.startup_timeout)
        api = ApiSession(config.server_url)
        api.visit(f"/auth/github/callback?redirectTo={urllib.parse.quote(config.web_url + '/dashboard', safe='')}")
        auth = api.request("GET", "/auth/session")
        if not auth.get("authenticated") or not auth.get("admin"):
            raise RuntimeError("local admin bootstrap failed")
        created = api.request("POST", "/admin/workers", {"name": "Local Debug Worker", "region": "local", "version": "0.10.24"})
        worker_id = str(created.get("worker_id") or "")
        worker_token = str(created.get("worker_token") or "")
        if not worker_id or not worker_token:
            raise RuntimeError("local Worker bootstrap returned no id or token")
        worker_specs = worker_process_specs(config, node, worker_id, worker_token)
        processes.append(spawn(worker_specs[0], run_root))
        time.sleep(1)
        processes.append(spawn(worker_specs[1], run_root))
        deadline = time.monotonic() + args.startup_timeout
        while time.monotonic() < deadline:
            workers = api.request("GET", "/admin/workers").get("items") or []
            if any(item.get("worker_id") == worker_id and item.get("last_heartbeat_at") for item in workers):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("local Worker did not heartbeat before the startup deadline")
        smoke = run_core_smoke(api, config.web_url, worker_id)
        report = {
            "schemaVersion": "pullwise-local-debug-report/v1",
            "status": smoke["status"],
            "runRoot": str(run_root),
            "urls": {"server": config.server_url, "web": config.web_url, "admin": config.admin_url},
            "entryUrls": entry_urls,
            "runtime": public_runtime_state(specs + worker_specs, worker_id),
            "smoke": smoke,
        }
        write_report(report_path, report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if args.hold and not args.no_open_browser:
            open_local_browser_entries(entry_urls)
        if args.hold:
            while all(process.poll() is None for process in processes):
                time.sleep(1)
            raise RuntimeError("a local debug process exited unexpectedly")
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        report = {"schemaVersion": "pullwise-local-debug-report/v1", "status": "fail", "error": str(exc), "runRoot": str(run_root)}
        write_report(report_path, report)
        print(json.dumps(report, ensure_ascii=False), file=sys.stderr, flush=True)
        return 1
    finally:
        stop_processes(processes)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-port", type=int, default=8080)
    parser.add_argument("--web-port", type=int, default=5173)
    parser.add_argument("--admin-port", type=int, default=5174)
    parser.add_argument("--admin-email", default="local-admin@pullwise.test")
    parser.add_argument("--profile-root", help="Existing Pi profile root; secrets remain in that directory.")
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    parser.add_argument("--hold", action="store_true", help="Keep all four projects running until interrupted.")
    parser.add_argument("--no-open-browser", action="store_true", help="Do not open authenticated Web/Admin tabs.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
