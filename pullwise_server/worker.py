"""
Background scan worker.

`POST /scans` inserts a queued record then calls `start_scan(scan_id)` here.
The worker walks a fixed phase progression so the frontend can render a
progress bar, calls into `review.run_review()` during the `ai` phase,
appends findings to the ISSUES blob, and marks the scan done.

Concurrency note: this worker mutates the same in-memory blobs (SCANS,
ISSUES) that request handlers read. The worker takes `app.STATE_LOCK` for
each mutation; readers see eventually-consistent state. Pre-existing race
windows in handler-handler interleaving are not addressed here.
"""

from __future__ import annotations

import threading
import time
import traceback

from . import review


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


def start_scan(scan_id: str) -> None:
    """Spawn a daemon thread to process the scan with the given id."""
    thread = threading.Thread(
        target=_run, args=(scan_id,), name=f"scan-{scan_id}", daemon=True
    )
    thread.start()


def _run(scan_id: str) -> None:
    # Lazy import to avoid a circular import at module load time.
    from . import app

    started_at = int(time.time())

    snapshot = _start_running(scan_id, started_at)
    if snapshot is None:
        return

    try:
        cumulative = 0.0
        total_weight = sum(w for _, w in PHASES if w is not None) + _AI_WEIGHT

        for phase_key, weight in PHASES:
            if _is_cancelled(scan_id):
                return

            if phase_key == "ai":
                findings = review.run_review(
                    repo=snapshot["repo"],
                    branch=snapshot["branch"],
                    commit=snapshot["commit"],
                    user_id=snapshot["userId"],
                    scan_id=scan_id,
                    repo_path=snapshot.get("repoPath"),
                )
                cumulative += _AI_WEIGHT
                _patch_scan(
                    scan_id,
                    {
                        "issues": _summarize(findings),
                        "progress": min(99, int(cumulative / total_weight * 100)),
                        "phase": phase_key,
                    },
                    extra_findings=findings,
                )
            else:
                time.sleep(weight or 0)
                cumulative += weight or 0
                _patch_scan(
                    scan_id,
                    {
                        "progress": min(99, int(cumulative / total_weight * 100)),
                        "phase": phase_key,
                    },
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

    except Exception as exc:
        traceback.print_exc()
        _patch_scan(
            scan_id,
            {
                "status": "failed",
                "error": str(exc)[:500],
                "completedAt": int(time.time()),
            },
            allow_after_cancel=True,
        )


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
        app.persist_state()
        return {
            "userId": scan["userId"],
            "repo": scan.get("repo", ""),
            "branch": scan.get("branch", "main"),
            "commit": scan.get("commit", "pending"),
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


def _summarize(findings: list[dict]) -> dict:
    summary = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for finding in findings:
        severity = finding.get("severity") or "low"
        if severity in summary:
            summary[severity] += 1
    return summary
