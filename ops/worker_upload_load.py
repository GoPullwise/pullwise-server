from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import statistics
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a local Pullwise v1 worker artifact upload load probe.",
    )
    parser.add_argument("--workers", type=int, default=24, help="Number of simulated workers/runs to seed.")
    parser.add_argument("--uploads", type=int, default=240, help="Total artifact upload requests to send.")
    parser.add_argument("--concurrency", type=int, default=24, help="Maximum concurrent HTTP uploads.")
    parser.add_argument("--artifact-kib", type=int, default=128, help="Decoded artifact content size per upload, in KiB.")
    parser.add_argument("--host", default="127.0.0.1", help="Local bind host for the in-process test server.")
    parser.add_argument("--port", type=int, default=0, help="Local bind port. 0 asks the OS for a free port.")
    parser.add_argument("--gzip", action=argparse.BooleanOptionalAction, default=True, help="Gzip the JSON request body.")
    parser.add_argument("--keep-db", type=Path, default=None, help="Optional SQLite path to keep after the run.")
    return parser.parse_args()


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[index]


def json_post(url: str, token: str, payload: dict[str, Any], *, use_gzip: bool) -> tuple[int, bytes, float]:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "pullwise-worker-upload-load/1",
    }
    if use_gzip:
        headers["X-Pullwise-Uncompressed-Length"] = str(len(raw))
        raw = gzip.compress(raw)
        headers["Content-Encoding"] = "gzip"
    started = time.perf_counter()
    request = urllib.request.Request(url, data=raw, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            return int(response.status), body, time.perf_counter() - started
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(), time.perf_counter() - started


def artifact_payload(run: dict[str, Any], upload_index: int, artifact_kib: int) -> dict[str, Any]:
    worker_id = str(run["worker_id"])
    run_id = str(run["run_id"])
    artifact_id = f"art_load_{upload_index:06d}"
    target_size = max(0, artifact_kib) * 1024
    header = json.dumps({"run_id": run_id, "worker_id": worker_id, "upload_index": upload_index}, sort_keys=True)
    padding_size = max(0, target_size - len(header) - len('{"padding":""}'))
    content = json.dumps(
        {
            "run_id": run_id,
            "worker_id": worker_id,
            "upload_index": upload_index,
            "padding": "x" * padding_size,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "protocol_version": "review-worker-protocol/v1",
        "attempt_id": str(run["attempt_id"]),
        "run_id": run_id,
        "artifact": {
            "artifact_id": artifact_id,
            "kind": "report.agent",
            "name": f"{artifact_id}.json",
            "media_type": "application/json",
            "schema_id": "codex-full-repo-review",
            "schema_version": "v1",
            "encoding": "utf-8",
            "compression": "none",
            "required": True,
            "storage": {"type": "server_artifact", "url": f"/v1/review-runs/{run_id}/artifacts/{artifact_id}"},
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        },
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def seed_runs(app: Any, db: Any, worker_count: int) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for index in range(1, worker_count + 1):
        worker_id = f"wk_load_{index:03d}"
        worker = db.create_worker({"worker_id": worker_id, "name": worker_id, "provider": "codex"})
        token = str(worker["worker_token"])
        db.upsert_worker_heartbeat(
            {
                "worker_id": worker_id,
                "version": "load-probe",
                "provider": "codex",
                "provider_chain": ["codex"],
                "running_jobs": 0,
                "doctor_status": "ok",
                "codex_ready": 1,
                "ready_providers": ["codex"],
                "timestamp": app.now(),
            }
        )
        job_id = f"job_load_{index:03d}"
        db.create_scan_job(
            {
                "job_id": job_id,
                "scan_id": f"sc_load_{index:03d}",
                "repo": f"acme/load-{index:03d}",
                "branch": "main",
                "commit": "abc1234",
                "status": "queued",
                "created_at": app.now() + index,
                "user_id": "usr_load",
                "max_attempts": 1,
            }
        )
        job = db.claim_next_scan_job(worker_id, ready_providers=["codex"], recover_before_claim=False)
        if not job:
            raise RuntimeError(f"failed to claim seeded job for {worker_id}")
        run_id = app.scan_job_run_id(job)
        runs.append(
            {
                "worker_id": worker_id,
                "token": token,
                "job_id": job["job_id"],
                "run_id": run_id,
                "attempt_id": f"{worker_id}-{int(job.get('attempt') or 1)}",
            }
        )
    return runs


def main() -> int:
    args = parse_args()
    if args.workers < 1 or args.uploads < 1 or args.concurrency < 1:
        raise SystemExit("--workers, --uploads, and --concurrency must be positive")

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if args.keep_db is None:
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db_path = Path(temp_dir.name) / "pullwise-load.sqlite3"
    else:
        db_path = args.keep_db
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            db_path.unlink()

    os.environ["PULLWISE_DB_PATH"] = str(db_path)
    os.environ["PULLWISE_MAX_BODY_BYTES"] = os.environ.get("PULLWISE_MAX_BODY_BYTES", str(1024 * 1024))
    os.environ["PULLWISE_MAX_DECOMPRESSED_BODY_BYTES"] = os.environ.get("PULLWISE_MAX_DECOMPRESSED_BODY_BYTES", str(50 * 1024 * 1024))

    from pullwise_server import app, db

    app.STATE_LOADED = True
    app.STATE_DIRTY = False
    app.USERS = {"usr_load": {"id": "usr_load", "name": "Load Probe", "email": "load@example.test"}}
    app.SESSIONS = {}
    app.SETTINGS = {}
    app.BILLING_EVENTS = {}
    app.BILLING_PENDING_UPDATES = []
    app.SCANS = []
    app.ISSUES = []
    db.ensure_initialized()

    runs = seed_runs(app, db, args.workers)
    httpd = ThreadingHTTPServer((args.host, args.port), app.PullwiseHandler)
    host, port = httpd.server_address[:2]
    base_url = f"http://{host}:{port}"
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    next_upload = 0
    next_lock = threading.Lock()
    result_lock = threading.Lock()
    latencies: list[float] = []
    statuses: dict[int, int] = {}
    errors: list[str] = []
    started = time.perf_counter()

    def worker() -> None:
        nonlocal next_upload
        while True:
            with next_lock:
                if next_upload >= args.uploads:
                    return
                upload_index = next_upload
                next_upload += 1
            run = runs[upload_index % len(runs)]
            payload = artifact_payload(run, upload_index, args.artifact_kib)
            status, body, elapsed = json_post(
                f"{base_url}/v1/review-runs/{run['run_id']}/artifacts",
                str(run["token"]),
                payload,
                use_gzip=args.gzip,
            )
            with result_lock:
                latencies.append(elapsed)
                statuses[status] = statuses.get(status, 0) + 1
                if status != 200:
                    errors.append(f"upload {upload_index}: HTTP {status}: {body[:200].decode('utf-8', errors='replace')}")

    threads = [threading.Thread(target=worker) for _ in range(args.concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started

    httpd.shutdown()
    httpd.server_close()
    if temp_dir is not None:
        temp_dir.cleanup()

    success = statuses.get(200, 0)
    print(json.dumps(
        {
            "workers": args.workers,
            "uploads": args.uploads,
            "concurrency": args.concurrency,
            "artifact_kib": args.artifact_kib,
            "gzip": args.gzip,
            "elapsed_seconds": round(elapsed, 3),
            "requests_per_second": round(args.uploads / elapsed, 2) if elapsed > 0 else 0,
            "success": success,
            "status_counts": {str(key): value for key, value in sorted(statuses.items())},
            "latency_ms": {
                "min": round(min(latencies) * 1000, 2) if latencies else 0,
                "p50": round(statistics.median(latencies) * 1000, 2) if latencies else 0,
                "p95": round(percentile(latencies, 95) * 1000, 2),
                "p99": round(percentile(latencies, 99) * 1000, 2),
                "max": round(max(latencies) * 1000, 2) if latencies else 0,
            },
            "db_path": str(db_path) if args.keep_db is not None else None,
            "errors": errors[:10],
        },
        indent=2,
        sort_keys=True,
    ))
    return 0 if success == args.uploads else 1


if __name__ == "__main__":
    sys.exit(main())