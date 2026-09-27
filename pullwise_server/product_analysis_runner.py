"""Claim-bound product analysis composition for an injected raw provider.

No runtime instantiates this by default. A production provider adapter must
prove its own total exit deadline before local analysis admission is enabled.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping

from .product_jobs import ProductJobExecutor
from .product_store import ProductStore
from .typesafe_client import DEFAULT_JEV_MODEL


_PUBLISHERS = {
    "ci_failure": "publish_claimed_ci_response",
    "pr_review_comment": "publish_claimed_pr_thread_response",
    "pr_comment": "publish_claimed_pr_comment_response",
    "pr_review_body": "publish_claimed_pr_review_response",
    "release": "publish_claimed_release_response",
}


class ProductAnalysisRunner:
    """Run one fenced job through admission, raw response and publication."""

    def __init__(self, store: ProductStore, *, invoke_raw: Callable,
                 clock: Callable[[], int], owner_monthly_limit: int,
                 global_monthly_limit: int, owner_rolling_limit: int,
                 global_rolling_limit: int, lease_seconds: int = 120,
                 retry_seconds: int = 60) -> None:
        if not callable(invoke_raw) or not callable(clock):
            raise ValueError("provider and clock must be callable")
        if type(lease_seconds) is not int or lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if type(retry_seconds) is not int or retry_seconds < 1:
            raise ValueError("retry_seconds must be positive")
        self.executor = ProductJobExecutor(store)
        self.invoke_raw = invoke_raw
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.retry_seconds = retry_seconds
        self.limits = {
            "owner_monthly_limit": owner_monthly_limit,
            "global_monthly_limit": global_monthly_limit,
            "owner_rolling_limit": owner_rolling_limit,
            "global_rolling_limit": global_rolling_limit,
        }

    def _now(self) -> int:
        value = self.clock()
        if type(value) is not int or value < 1:
            raise ValueError("analysis clock must return a positive Unix second")
        return value

    def _fail(self, claim: Mapping[str, object], *, retryable: bool) -> None:
        now = self._now()
        try:
            self.executor.record_analysis_failure(
                job_id=claim["id"], claim_token=claim["claimToken"], now=now,
                retryable=retryable,
                next_attempt_at=now + self.retry_seconds if retryable else None,
            )
        except ValueError as error:
            # A newer worker or expired lease owns recovery after claim loss.
            if str(error) not in {"JOB_CLAIM_LOST", "JOB_CLAIM_EXPIRED"}:
                raise

    def run_once(self) -> dict | None:
        claim = self.executor.claim_next_analysis(now=self._now(),
            lease_seconds=self.lease_seconds)
        return self.process_claim(claim) if claim is not None else None

    def process_claim(self, claim: Mapping[str, object]) -> dict:
        if not isinstance(claim, Mapping) or not isinstance(claim.get("id"), str) or not isinstance(
                claim.get("claimToken"), str):
            raise ValueError("claimed analysis job is invalid")
        job_id, claim_token = claim["id"], claim["claimToken"]
        try:
            prepared = self.executor.prepare_claimed_analysis(
                job_id=job_id, claim_token=claim_token, now=self._now())
            attempt = self.executor.admit_claimed_provider_attempt(
                job_id=job_id, claim_token=claim_token,
                input_key=prepared["inputHash"], now=self._now(), **self.limits)
            if attempt["reused"]:
                raise ValueError("PROVIDER_ATTEMPT_ALREADY_USED")
            publisher_name = _PUBLISHERS[prepared["source"]["sourceType"]]
        except Exception:
            self._fail(claim, retryable=False)
            raise

        try:
            raw_body = self.invoke_raw(state=prepared["state"],
                questions=prepared["questions"], model=DEFAULT_JEV_MODEL)
        except Exception:
            self._fail(claim, retryable=True)
            raise

        try:
            return getattr(self.executor, publisher_name)(
                job_id=job_id, claim_token=claim_token,
                raw_body=raw_body, observed_at=self._now())
        except Exception:
            self._fail(claim, retryable=False)
            raise
