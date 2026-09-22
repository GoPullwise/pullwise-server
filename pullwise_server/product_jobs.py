from __future__ import annotations

from enum import Enum
from typing import Mapping, Sequence

from .product_store import ProductStore


class TrustedTrigger(str, Enum):
    GITHUB_EVENT = "github_event"
    SCHEDULED_DISCOVERY = "scheduled_discovery"
    INITIAL_BACKFILL = "initial_backfill"
    CONTEXT_REFRESH = "context_refresh"
    RETRY = "retry"


class ProductJobScheduler:
    def __init__(self, store: ProductStore) -> None:
        self.store = store

    def request_manual_sync(
        self,
        *,
        resource_kind: str,
        resource_id: str,
        requester_id: str,
    ) -> dict:
        if resource_kind == "repository":
            job_type = "sync_repository"
        elif resource_kind == "watch":
            job_type = "sync_watch"
        else:
            raise ValueError("manual sync supports repository or watch resources")
        return self.store.enqueue_background_job(
            job_type=job_type,
            logical_key=f"{job_type}:{resource_id}",
            trusted_trigger="manual_sync",
            requester_id=requester_id,
        )

    def schedule_analysis(
        self,
        *,
        source_context_key: str,
        source_id: str,
        context_id: str,
        reservation_id: str,
        trigger: TrustedTrigger,
        global_active_limit: int = 1000,
        owner_active_limit: int = 100,
    ) -> dict:
        if not isinstance(trigger, TrustedTrigger):
            raise ValueError("analysis scheduling requires a trusted internal trigger")
        result = self.store.enqueue_background_job(
            job_type="analyze_source",
            logical_key=f"analyze_source:{source_context_key}",
            trusted_trigger=trigger.value,
            requester_id=None,
            source_id=source_id,
            context_id=context_id,
            reservation_id=reservation_id,
            global_active_limit=global_active_limit,
            owner_active_limit=owner_active_limit,
        )
        if result.get("rejected"):
            raise ValueError(result["code"])
        return result


class ProductJobExecutor:
    """Executes already-validated analysis results without exposing a user entrypoint."""

    def __init__(self, store: ProductStore) -> None:
        self.store = store

    def claim_next_analysis(self, *, now: int, lease_seconds: int = 120) -> dict | None:
        return self.store.claim_next_analysis_job(now=now, lease_seconds=lease_seconds)

    def publish_validated_assessment(
        self,
        *,
        job_id: str,
        claim_token: str,
        assessment: Mapping[str, object],
        item_id: str,
        expected_item_revision: int,
        sources: Sequence[Mapping[str, object]],
        context_fences: Sequence[Mapping[str, object]],
        snapshot: Mapping[str, object],
        observed_at: int,
    ) -> dict:
        job = self.store.get_background_job(job_id)
        if job is None:
            raise ValueError("JOB_CLAIM_LOST")
        return self.store.publish_assessment_result(
            job_id=job_id,
            claim_token=claim_token,
            reservation_id=job.get("reservationId"),
            assessment=assessment,
            item_id=item_id,
            expected_item_revision=expected_item_revision,
            sources=sources,
            context_fences=context_fences,
            snapshot=snapshot,
            observed_at=observed_at,
        )

    def record_analysis_failure(
        self,
        *,
        job_id: str,
        claim_token: str,
        now: int,
        retryable: bool,
        next_attempt_at: int | None,
    ) -> dict:
        return self.store.record_analysis_failure(
            job_id=job_id,
            claim_token=claim_token,
            now=now,
            retryable=retryable,
            next_attempt_at=next_attempt_at,
        )
