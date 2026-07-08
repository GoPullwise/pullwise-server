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

OPERATIONS = {"artifact", "event", "heartbeat", "result", "mixed"}
REQUIRED_COMPLETED_ARTIFACTS = (
    ("art_report_human", "report.human", "report.md", "text/markdown", "human-markdown-report"),
    ("art_report_agent", "report.agent", "report.agent.json", "application/json", "codex-full-repo-review"),
    ("art_coverage", "coverage", "coverage.json", "application/json", "coverage"),
    ("art_qa", "qa", "qa.json", "application/json", "qa-gate"),
    ("art_token_budget", "token_budget", "token-budget.json", "application/json", "token-budget"),
)
PROGRESS_COUNTERS = {
    "source_like_files_total": 0,
    "source_like_files_classified": 0,
    "bundles_total": 0,
    "bundles_packed": 0,
    "reviewer_runs_total": 0,
    "reviewer_runs_completed": 0,
    "intent_tests_total": 0,
    "intent_tests_written": 0,
    "intent_tests_run": 0,
    "validator_candidates_total": 0,
    "validator_candidates_completed": 0,
    "artifacts_total": 0,
    "artifacts_uploaded": 0,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a local Pullwise v1 worker control-plane/upload load probe.",
    )
    parser.add_argument("--workers", type=int, default=24, help="Number of simulated workers/runs to seed.")
    parser.add_argument("--uploads", type=int, default=240, help="Total requests to send; kept for compatibility with the first artifact-only probe.")
    parser.add_argument("--concurrency", type=int, default=24, help="Maximum concurrent HTTP requests.")
    parser.add_argument("--operation", choices=sorted(OPERATIONS), default="artifact", help="Worker endpoint family to stress.")
    parser.add_argument("--artifact-kib", type=int, default=128, help="Decoded artifact content size per artifact upload, in KiB.")
    parser.add_argument("--event-kib", type=int, default=4, help="Approximate progress event JSON data padding, in KiB.")
    parser.add_argument("--host", default="127.0.0.1", help="Local bind host for the in-process test server.")
    parser.add_argument("--port", type=int, default=0, help="Local bind port. 0 asks the OS for a free port.")
    parser.add_argument("--gzip", action=argparse.BooleanOptionalAction, default=True, help="Gzip JSON request bodies.")
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
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        return 0, str(exc).encode("utf-8", errors="replace"), time.perf_counter() - started


def padded_json_bytes(payload: dict[str, Any], target_kib: int) -> bytes:
    target_size = max(0, target_kib) * 1024
    base = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    padding_size = max(0, target_size - len(base) - len(',"padding":""'))
    payload = dict(payload)
    payload["padding"] = "x" * padding_size
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def artifact_payload(run: dict[str, Any], request_index: int, artifact_kib: int) -> dict[str, Any]:
    worker_id = str(run["worker_id"])
    run_id = str(run["run_id"])
    artifact_id = f"art_load_{request_index:06d}"
    content = padded_json_bytes({"run_id": run_id, "worker_id": worker_id, "request_index": request_index}, artifact_kib)
    return {
        "protocol_version": "review-worker-protocol/v1",
        "attempt_id": str(run["attempt_id"]),
        "run_id": run_id,
        "artifact": artifact_item(run_id, artifact_id, "report.agent", f"{artifact_id}.json", "application/json", "codex-full-repo-review", content),
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def artifact_item(run_id: str, artifact_id: str, kind: str, name: str, media_type: str, schema_id: str, content: bytes) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "name": name,
        "media_type": media_type,
        "schema_id": schema_id,
        "schema_version": "v1",
        "encoding": "utf-8",
        "compression": "none",
        "required": True,
        "storage": {"type": "server_artifact", "url": f"/v1/review-runs/{run_id}/artifacts/{artifact_id}"},
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def progress_event_payload(run: dict[str, Any], sequence: int, event_kib: int) -> dict[str, Any]:
    data = json.loads(padded_json_bytes({"request_kind": "load_probe_event", "sequence": sequence}, event_kib).decode("utf-8"))
    return {
        "protocol_version": "review-worker-protocol/v1",
        "run_id": str(run["run_id"]),
        "worker_id": str(run["worker_id"]),
        "sequence": sequence,
        "timestamp": "2026-07-08T00:00:00Z",
        "event_type": "progress_updated",
        "phase": "reviewer_fanout",
        "severity": "info",
        "message": f"load probe event {sequence}",
        "progress": {
            "overall_percent": min(99, 10 + sequence % 80),
            "current_phase_percent": min(100, sequence % 101),
            "status": "running",
        },
        "data": data,
    }


def heartbeat_payload(run: dict[str, Any], request_index: int) -> dict[str, Any]:
    run_id = str(run["run_id"])
    return {
        "protocol_version": "review-worker-protocol/v1",
        "worker_id": str(run["worker_id"]),
        "status": "busy",
        "active_run_id": run_id,
        "version": "load-probe",
        "provider": "codex",
        "codex_ready": True,
        "ready_providers": ["codex"],
        "concurrency": {
            "max_active_jobs": 1,
            "active_jobs": 1,
            "available_job_slots": 0,
            "maintains_local_queue": False,
            "local_queue_depth": 0,
        },
        "codex_app_server": {
            "status": "ready",
            "transport": "stdio",
            "active_thread_id": f"thread_{run_id}",
        },
        "progress": {
            "run_id": run_id,
            "overall_percent": min(99, 10 + request_index % 80),
            "current_phase": "reviewer_fanout",
            "current_phase_status": "running",
            "current_phase_percent": min(100, request_index % 101),
            "status": "running",
            "message": f"load probe heartbeat {request_index}",
            "last_event_sequence": max(1, request_index + 1),
            "updated_at": "2026-07-08T00:00:00Z",
            "counters": dict(PROGRESS_COUNTERS),
            "active_unit": {"kind": "load_probe", "id": str(request_index), "label": "Load probe"},
        },
    }


def completed_manifest(run: dict[str, Any]) -> list[dict[str, Any]]:
    run_id = str(run["run_id"])
    items: list[dict[str, Any]] = []
    for artifact_id, kind, name, media_type, schema_id in REQUIRED_COMPLETED_ARTIFACTS:
        content = f"{kind}:{run_id}\n".encode("utf-8")
        items.append(artifact_item(run_id, artifact_id, kind, name, media_type, schema_id, content))
    return items


def result_payload(run: dict[str, Any]) -> dict[str, Any]:
    manifest = completed_manifest(run)
    envelope = {
        "protocol_version": "review-worker-protocol/v1",
        "message_type": "review_run_result",
        "job": {
            "job_id": str(run["job_id"]),
            "run_id": str(run["run_id"]),
            "lease_id": str(run["lease_id"]),
            "attempt_id": str(run["attempt_id"]),
        },
        "worker": {
            "worker_id": str(run["worker_id"]),
            "worker_version": "load-probe",
            "engine": {"type": "codex", "codex_thread_id": f"thread_{run['run_id']}"},
        },
        "execution": {
            "status": "completed",
            "started_at": "2026-07-08T00:00:00Z",
            "completed_at": "2026-07-08T00:01:00Z",
            "duration_ms": 60000,
        },
        "progress_final": {"overall_percent": 100, "current_phase_percent": 100, "status": "completed"},
        "quality_gate": {"status": "pass", "errors": [], "warnings": []},
        "artifact_manifest": manifest,
        "summary": {
            "overall_risk": "unknown",
            "result_status": "complete",
            "finding_counts": {
                "confirmed_critical": 0,
                "confirmed_high": 0,
                "confirmed_medium": 0,
                "confirmed_low": 0,
                "plausible": 0,
                "weak_appendix": 0,
                "disproven": 0,
                "suppressed": 0,
            },
            "coverage": {
                "source_like_files_total": 0,
                "deep_reviewed_files": 0,
                "standard_reviewed_files": 0,
                "light_reviewed_files": 0,
                "inventory_only_files": 0,
                "skipped_files": 0,
                "intent_tests_planned": 0,
                "intent_tests_run": 0,
            },
            "top_findings": [],
        },
        "usage": {},
    }
    return {
        "status": "done",
        "attempt_id": str(run["attempt_id"]),
        "result_checksum": f"checksum-{run['run_id']}",
        "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        "reviewWorkerProtocol": envelope,
    }


def seed_result_artifacts(db: Any, runs: list[dict[str, Any]]) -> None:
    for run in runs:
        for item in completed_manifest(run):
            content = f"{item['kind']}:{run['run_id']}\n".encode("utf-8")
            db.store_review_run_artifact(
                job_id=str(run["job_id"]),
                attempt_id=str(run["attempt_id"]),
                artifact_id=str(item["artifact_id"]),
                payload={
                    "protocol_version": "review-worker-protocol/v1",
                    "attempt_id": str(run["attempt_id"]),
                    "run_id": str(run["run_id"]),
                    "artifact": item,
                    "content_base64": base64.b64encode(content).decode("ascii"),
                },
            )


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
        scan_id = f"sc_load_{index:03d}"
        job_id = f"job_load_{index:03d}"
        app.SCANS.append(
            {
                "id": scan_id,
                "repo": f"acme/load-{index:03d}",
                "branch": "main",
                "commit": "abc1234",
                "status": "running",
                "userId": "usr_load",
                "createdAt": app.now() + index,
                "queuedAt": app.now() + index,
                "progress": 0,
                "phase": None,
            }
        )
        db.create_scan_job(
            {
                "job_id": job_id,
                "scan_id": scan_id,
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
                "lease_id": job.get("lease_id") or f"lease_{job['job_id']}",
                "attempt_id": f"{worker_id}-{int(job.get('attempt') or 1)}",
            }
        )
    return runs


def request_for_operation(base_url: str, run: dict[str, Any], operation: str, request_index: int, sequence: int, args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    if operation == "artifact":
        return f"{base_url}/v1/review-runs/{run['run_id']}/artifacts", artifact_payload(run, request_index, args.artifact_kib)
    if operation == "event":
        return f"{base_url}/v1/review-runs/{run['run_id']}/events", progress_event_payload(run, sequence, args.event_kib)
    if operation == "heartbeat":
        return f"{base_url}/v1/workers/{run['worker_id']}/heartbeat", heartbeat_payload(run, request_index)
    if operation == "result":
        return f"{base_url}/v1/review-runs/{run['run_id']}/result", result_payload(run)
    raise ValueError(f"unsupported operation: {operation}")


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
    if args.operation == "result":
        seed_result_artifacts(db, runs)
    httpd = ThreadingHTTPServer((args.host, args.port), app.PullwiseHandler)
    host, port = httpd.server_address[:2]
    base_url = f"http://{host}:{port}"
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    total_requests = min(args.uploads, len(runs)) if args.operation == "result" else args.uploads
    next_request = 0
    next_lock = threading.Lock()
    result_lock = threading.Lock()
    run_locks = {str(run["run_id"]): threading.Lock() for run in runs}
    run_sequences = {str(run["run_id"]): 0 for run in runs}
    latencies_by_operation: dict[str, list[float]] = {}
    statuses_by_operation: dict[str, dict[int, int]] = {}
    errors: list[str] = []
    started = time.perf_counter()

    def choose_operation(request_index: int) -> str:
        if args.operation != "mixed":
            return args.operation
        # Approximate a busy worker period: frequent heartbeats/progress, occasional artifacts.
        return ("heartbeat", "event", "event", "artifact")[request_index % 4]

    def worker() -> None:
        nonlocal next_request
        while True:
            with next_lock:
                if next_request >= total_requests:
                    return
                request_index = next_request
                next_request += 1
            run = runs[request_index % len(runs)] if args.operation != "result" else runs[request_index]
            operation = choose_operation(request_index)
            run_id = str(run["run_id"])
            lock = run_locks[run_id] if operation in {"event", "result"} else threading.Lock()
            with lock:
                if operation == "event":
                    run_sequences[run_id] += 1
                    sequence = run_sequences[run_id]
                else:
                    sequence = request_index + 1
                url, payload = request_for_operation(base_url, run, operation, request_index, sequence, args)
                status, body, elapsed = json_post(url, str(run["token"]), payload, use_gzip=args.gzip)
            with result_lock:
                latencies_by_operation.setdefault(operation, []).append(elapsed)
                statuses = statuses_by_operation.setdefault(operation, {})
                statuses[status] = statuses.get(status, 0) + 1
                if status != 200:
                    errors.append(f"{operation} {request_index}: HTTP {status}: {body[:200].decode('utf-8', errors='replace')}")

    threads = [threading.Thread(target=worker) for _ in range(min(args.concurrency, total_requests))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started

    httpd.shutdown()
    httpd.server_close()
    if temp_dir is not None:
        temp_dir.cleanup()

    all_statuses: dict[int, int] = {}
    for statuses in statuses_by_operation.values():
        for status, count in statuses.items():
            all_statuses[status] = all_statuses.get(status, 0) + count
    all_latencies = [value for values in latencies_by_operation.values() for value in values]
    success = all_statuses.get(200, 0)

    def latency_summary(values: list[float]) -> dict[str, float]:
        return {
            "min": round(min(values) * 1000, 2) if values else 0,
            "p50": round(statistics.median(values) * 1000, 2) if values else 0,
            "p95": round(percentile(values, 95) * 1000, 2),
            "p99": round(percentile(values, 99) * 1000, 2),
            "max": round(max(values) * 1000, 2) if values else 0,
        }

    print(json.dumps(
        {
            "workers": args.workers,
            "operation": args.operation,
            "requested": total_requests,
            "concurrency": args.concurrency,
            "artifact_kib": args.artifact_kib,
            "event_kib": args.event_kib,
            "gzip": args.gzip,
            "elapsed_seconds": round(elapsed, 3),
            "requests_per_second": round(total_requests / elapsed, 2) if elapsed > 0 else 0,
            "success": success,
            "status_counts": {str(key): value for key, value in sorted(all_statuses.items())},
            "operation_status_counts": {
                operation: {str(key): value for key, value in sorted(statuses.items())}
                for operation, statuses in sorted(statuses_by_operation.items())
            },
            "latency_ms": latency_summary(all_latencies),
            "operation_latency_ms": {
                operation: latency_summary(values)
                for operation, values in sorted(latencies_by_operation.items())
            },
            "db_path": str(db_path) if args.keep_db is not None else None,
            "errors": errors[:10],
        },
        indent=2,
        sort_keys=True,
    ))
    return 0 if success == total_requests else 1


if __name__ == "__main__":
    sys.exit(main())