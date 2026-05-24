"""
Background scan worker.

`POST /scans` inserts a queued record then calls `start_scan(scan_id)` here.
The worker walks a fixed phase progression so the frontend can render a
progress bar, calls into `review.run_review()` during the `ai` phase,
appends findings to the ISSUES blob, and marks the scan done.

Concurrency note: actual scan work is processed by a fixed worker pool and
claimed from queued scans only when global and per-user limits allow it. The
worker mutates the same in-memory blobs (SCANS, ISSUES) that request handlers
read, and takes `app.STATE_LOCK` for each mutation; readers see
eventually-consistent state. Pre-existing race windows in handler-handler
interleaving are not addressed here.
"""

from __future__ import annotations

import threading
import time
import traceback

from . import checkout, review, scan_logging


# Phase keys must match the SCAN_PHASES table the frontend renders.
PHASES: list[tuple[str, float | None]] = [
    ("clone",   0.6),
    ("index",   0.6),
    ("secrets", 0.4),
    ("deps",    0.6),
    ("ai",      None),   # weighted separately; provider sets the actual duration
    ("report",  0.3),
]
_AI_WEIGHT = 4.0
_QUEUE_CONDITION = threading.Condition()
_WORKER_THREADS: list[threading.Thread] = []
_WORKER_CONFIG: int | None = None


def start_scan(scan_id: str) -> None:
    """Ensure the fixed worker pool is running and wake it for queued work."""
    scan_logging.log_event("scan_start_requested", scanId=scan_id)
    ensure_workers()
    notify_queue_changed()


def ensure_workers() -> None:
    global _WORKER_CONFIG
    desired = _env_limit("PULLWISE_MAX_CONCURRENT_SCANS", 1)
    with _QUEUE_CONDITION:
        if _WORKER_CONFIG != desired:
            _WORKER_CONFIG = desired
        while len(_WORKER_THREADS) < desired:
            index = len(_WORKER_THREADS) + 1
            thread = threading.Thread(
                target=_worker_loop,
                name=f"scan-worker-{index}",
                daemon=True,
            )
            _WORKER_THREADS.append(thread)
            thread.start()
        _QUEUE_CONDITION.notify_all()


def notify_queue_changed() -> None:
    with _QUEUE_CONDITION:
        _QUEUE_CONDITION.notify_all()


def _worker_loop() -> None:
    while True:
        snapshot = _wait_for_next_scan()
        _execute_scan(snapshot["id"], snapshot, int(snapshot["startedAt"]))
        notify_queue_changed()


def _wait_for_next_scan() -> dict:
    while True:
        snapshot = _claim_next_scan()
        if snapshot:
            return snapshot
        with _QUEUE_CONDITION:
            _QUEUE_CONDITION.wait(timeout=1)


def _claim_next_scan() -> dict | None:
    from . import app

    with app.STATE_LOCK:
        running = [scan for scan in app.SCANS if scan.get("status") == "running"]
        if len(running) >= _env_limit("PULLWISE_MAX_CONCURRENT_SCANS", 1):
            return None

        per_user_limit = _env_limit("PULLWISE_MAX_CONCURRENT_SCANS_PER_USER", 1)
        running_by_user: dict[str, int] = {}
        for scan in running:
            user_id = str(scan.get("userId") or "")
            running_by_user[user_id] = running_by_user.get(user_id, 0) + 1

        for scan in sorted(_queued_scans(app.SCANS), key=_scan_queue_sort_key):
            user_id = str(scan.get("userId") or "")
            if running_by_user.get(user_id, 0) >= per_user_limit:
                _log_scan_event(
                    "queue_deferred",
                    str(scan.get("id") or ""),
                    scan,
                    reason="user_limit",
                    runningForUser=running_by_user.get(user_id, 0),
                    perUserLimit=per_user_limit,
                )
                continue
            snapshot = _mark_running_locked(scan)
            _log_scan_event(
                "queue_claimed",
                snapshot["id"],
                snapshot,
                runningGlobal=len(running),
                globalLimit=_env_limit("PULLWISE_MAX_CONCURRENT_SCANS", 1),
                perUserLimit=per_user_limit,
            )
            return snapshot
    return None


def _run(scan_id: str) -> None:
    started_at = int(time.time())
    snapshot = _start_running(scan_id, started_at)
    if snapshot:
        _execute_scan(scan_id, snapshot, started_at)


def _execute_scan(scan_id: str, snapshot: dict, started_at: int) -> None:
    _log_scan_event(
        "scan_started",
        scan_id,
        snapshot,
        provider=review.selected_provider(),
        checkoutRequired=review.provider_requires_checkout(),
        startedAt=started_at,
    )
    try:
        cumulative = 0.0
        total_weight = sum(w for _, w in PHASES if w is not None) + _AI_WEIGHT

        for phase_key, weight in PHASES:
            if _is_cancelled(scan_id):
                _log_scan_event("scan_cancelled", scan_id, snapshot, phase=phase_key)
                return

            _log_scan_event("phase_started", scan_id, snapshot, phase=phase_key)
            if phase_key == "clone":
                repo_path = _prepare_checkout_if_needed(scan_id, snapshot)
                if repo_path:
                    snapshot["repoPath"] = repo_path
                    _log_scan_event("checkout_ready", scan_id, snapshot, repoPath=repo_path)
                else:
                    time.sleep(weight or 0)
                cumulative += weight or 0
                patch = {
                    "progress": min(99, int(cumulative / total_weight * 100)),
                    "phase": phase_key,
                }
                if repo_path:
                    patch["repoPath"] = repo_path
                _patch_scan(scan_id, patch)
                _log_scan_event(
                    "phase_completed",
                    scan_id,
                    snapshot,
                    phase=phase_key,
                    progress=patch["progress"],
                )
            elif phase_key == "ai":
                review_started_at = time.monotonic()
                _log_scan_event(
                    "review_started",
                    scan_id,
                    snapshot,
                    provider=review.selected_provider(),
                    repoPath=snapshot.get("repoPath"),
                )
                findings = review.run_review(
                    repo=snapshot["repo"],
                    branch=snapshot["branch"],
                    commit=snapshot["commit"],
                    user_id=snapshot["userId"],
                    scan_id=scan_id,
                    repo_path=snapshot.get("repoPath"),
                )
                review_duration_ms = int((time.monotonic() - review_started_at) * 1000)
                summary = _summarize(findings)
                _log_scan_event(
                    "review_completed",
                    scan_id,
                    snapshot,
                    provider=review.selected_provider(),
                    findingCount=len(findings),
                    durationMs=review_duration_ms,
                    critical=summary["critical"],
                    high=summary["high"],
                    medium=summary["medium"],
                    low=summary["low"],
                    info=summary["info"],
                )
                cumulative += _AI_WEIGHT
                _patch_scan(
                    scan_id,
                    {
                        "issues": summary,
                        "progress": min(99, int(cumulative / total_weight * 100)),
                        "phase": phase_key,
                    },
                    extra_findings=findings,
                )
                _log_scan_event(
                    "phase_completed",
                    scan_id,
                    snapshot,
                    phase=phase_key,
                    progress=min(99, int(cumulative / total_weight * 100)),
                )
            else:
                time.sleep(weight or 0)
                cumulative += weight or 0
                progress = min(99, int(cumulative / total_weight * 100))
                _patch_scan(scan_id, {"progress": progress, "phase": phase_key})
                _log_scan_event(
                    "phase_completed",
                    scan_id,
                    snapshot,
                    phase=phase_key,
                    progress=progress,
                )

        completed_at = int(time.time())
        _patch_scan(
            scan_id,
            {
                "status": "done",
                "phase": "report",
                "progress": 100,
                "completedAt": completed_at,
                "durationMs": (completed_at - started_at) * 1000,
            },
        )
        _log_scan_event(
            "scan_completed",
            scan_id,
            snapshot,
            completedAt=completed_at,
            durationMs=(completed_at - started_at) * 1000,
        )

    except checkout.CheckoutCancelled:
        _log_scan_event("scan_cancelled", scan_id, snapshot, reason="checkout_cancelled")
        return
    except Exception as exc:
        if _is_cancelled(scan_id):
            _log_scan_event("scan_cancelled", scan_id, snapshot, reason="cancelled_after_error")
            return
        traceback.print_exc()
        _log_scan_event("scan_failed", scan_id, snapshot, error=str(exc)[:500])
        _patch_scan(
            scan_id,
            {
                "status": "failed",
                "error": str(exc)[:500],
                "completedAt": int(time.time()),
            },
            allow_after_cancel=True,
        )
    finally:
        _cleanup_checkout_workspace(scan_id, snapshot)


def _mark_running_locked(scan: dict) -> dict:
    from . import app

    started_at = int(time.time())
    scan.update(
        {
            "status": "running",
            "startedAt": started_at,
            "progress": 0,
            "phase": PHASES[0][0],
        }
    )
    app.mark_state_dirty()
    app.persist_state()
    return _scan_snapshot(scan, started_at)


def _scan_snapshot(scan: dict, started_at: int) -> dict:
    return {
        "id": scan["id"],
        "userId": scan["userId"],
        "repo": scan.get("repo", ""),
        "branch": scan.get("branch", "main"),
        "commit": scan.get("commit", "pending"),
        "installationId": scan.get("installationId"),
        "cloneUrl": scan.get("cloneUrl"),
        "repoPath": scan.get("repoPath"),
        "startedAt": started_at,
    }


def _queued_scans(scans: list[dict]) -> list[dict]:
    return [scan for scan in scans if scan.get("status") == "queued"]


def _scan_queue_sort_key(scan: dict) -> tuple[int, str]:
    return (
        int(scan.get("queuedAt") or scan.get("createdAt") or 0),
        str(scan.get("id") or ""),
    )


def _env_limit(name: str, default: int) -> int:
    from . import app

    return max(1, app.env_int(name, default))


def _scan_user_id(scan_id: str) -> str | None:
    from . import app

    with app.STATE_LOCK:
        scan = _find_scan(app.SCANS, scan_id)
        if scan is None or scan.get("status") == "cancelled":
            return None
        return str(scan.get("userId") or "") or None


def _start_running(scan_id: str, started_at: int) -> dict | None:
    from . import app

    with app.STATE_LOCK:
        scan = _find_scan(app.SCANS, scan_id)
        if scan is None or scan.get("status") == "cancelled":
            return None
        scan.update(
            {
                "status": "running",
                "startedAt": started_at,
                "progress": 0,
                "phase": PHASES[0][0],
            }
        )
        app.mark_state_dirty()
        app.persist_state()
        return {
            "userId": scan["userId"],
            "repo": scan.get("repo", ""),
            "branch": scan.get("branch", "main"),
            "commit": scan.get("commit", "pending"),
            "installationId": scan.get("installationId"),
            "cloneUrl": scan.get("cloneUrl"),
            "repoPath": scan.get("repoPath"),
        }


def _patch_scan(
    scan_id: str,
    patch: dict,
    *,
    extra_findings: list[dict] | None = None,
    allow_after_cancel: bool = False,
) -> None:
    from . import app

    with app.STATE_LOCK:
        scan = _find_scan(app.SCANS, scan_id)
        if scan is None:
            return
        if scan.get("status") == "cancelled" and not allow_after_cancel:
            return
        scan.update(patch)
        if extra_findings:
            app.ISSUES.extend(extra_findings)
        app.mark_state_dirty()
        app.persist_state()


def _is_cancelled(scan_id: str) -> bool:
    from . import app

    with app.STATE_LOCK:
        scan = _find_scan(app.SCANS, scan_id)
        return scan is None or scan.get("status") == "cancelled"


def _find_scan(scans: list[dict], scan_id: str) -> dict | None:
    for scan in scans:
        if scan.get("id") == scan_id:
            return scan
    return None


def _prepare_checkout_if_needed(scan_id: str, snapshot: dict) -> str | None:
    if not review.provider_requires_checkout():
        return None
    repo_path = snapshot.get("repoPath")
    if repo_path and checkout.path_in_scan_workspace(
        str(repo_path),
        str(snapshot.get("userId") or ""),
        scan_id,
    ):
        return str(repo_path)
    return checkout.prepare_checkout(scan_id, snapshot, lambda: _is_cancelled(scan_id))


def _cleanup_checkout_workspace(scan_id: str, snapshot: dict) -> None:
    from . import app

    user_id = str(snapshot.get("userId") or "")
    if not user_id:
        return
    with app.preview_scan_lock(scan_id):
        try:
            _log_scan_event("cleanup_started", scan_id, snapshot)
            checkout.cleanup_scan_workspace(user_id, scan_id)
            _patch_scan(scan_id, {"repoPath": None}, allow_after_cancel=True)
            _log_scan_event("cleanup_completed", scan_id, snapshot)
        except Exception:
            traceback.print_exc()
            _log_scan_event("cleanup_failed", scan_id, snapshot)


def _log_scan_event(event: str, scan_id: str, snapshot: dict, **fields: object) -> None:
    scan_logging.log_event(
        event,
        scanId=scan_id,
        userId=snapshot.get("userId"),
        repo=snapshot.get("repo"),
        branch=snapshot.get("branch"),
        commit=snapshot.get("commit"),
        **fields,
    )


def _summarize(findings: list[dict]) -> dict:
    summary = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for finding in findings:
        severity = finding.get("severity") or "low"
        if severity in summary:
            summary[severity] += 1
    return summary
