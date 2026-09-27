from __future__ import annotations

from enum import Enum
from typing import Mapping, Sequence

from .product_analysis_input import prepare_source_questions
from .ci_triage_policy import AUTO_LABEL_POLICY, publish_symptom
from .product_store import ProductStore
from .pr_followup import _ACTIONS, _segments
from .typesafe_client import DEFAULT_JEV_MODEL, validate_response


class TrustedTrigger(str, Enum):
    GITHUB_EVENT = "github_event"
    SCHEDULED_DISCOVERY = "scheduled_discovery"
    INITIAL_BACKFILL = "initial_backfill"
    CONTEXT_REFRESH = "context_refresh"
    RETRY = "retry"


class ProductJobScheduler:
    def __init__(self, store: ProductStore) -> None:
        self.store = store

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

    def prepare_claimed_analysis(self, *, job_id: str, claim_token: str, now: int) -> dict:
        claimed = self.store.read_claimed_analysis_input(
            job_id=job_id, claim_token=claim_token, now=now)
        prepared = prepare_source_questions(claimed["source"], interests=claimed["interests"])
        return {**prepared, "source": claimed["source"], "context": claimed["context"],
                "module": claimed["module"], "item": claimed["item"],
                "dependencies": claimed["dependencies"],
                "dependencyFences": claimed["dependencyFences"]}

    def publish_claimed_ci_response(
        self, *, job_id: str, claim_token: str, raw_body: bytes, observed_at: int,
    ) -> dict:
        """Attach visible CI symptoms to the current rule Item after strict validation."""
        prepared = self.prepare_claimed_analysis(
            job_id=job_id, claim_token=claim_token, now=observed_at)
        source, context, item = prepared["source"], prepared["context"], prepared["item"]
        if source["sourceType"] != "ci_failure" or item is None or prepared["module"] != "ci":
            raise ValueError("CLAIMED_CI_ITEM_REQUIRED")
        self.store.require_claimed_provider_attempt(job_id=job_id, claim_token=claim_token,
            billing_owner_id=context["billingOwnerId"], input_key=prepared["inputHash"])
        response = validate_response(raw_body,
            expected_questions={key: tuple(question["criteria"])
                for key, question in prepared["questions"].items()},
            requested_model=DEFAULT_JEV_MODEL)
        evidence_by_window: dict[str, list[str]] = {}
        symptoms_by_window: dict[str, set[str]] = {}
        for question_id, binding in prepared["bindings"].items():
            window_id = binding["windowId"]
            evidence_by_window.setdefault(window_id, []).extend(binding["evidenceIds"])
            answer = response["answers"][question_id]
            if publish_symptom(answer):
                symptoms_by_window.setdefault(window_id, set()).add(question_id.split("_", 1)[1])
        snapshot = dict(item["snapshot"])
        if snapshot.get("module") != "ci" or "investigate_failure" not in snapshot.get("actionTypes", []):
            raise ValueError("CI_ITEM_SNAPSHOT_INVALID")
        facts = dict(snapshot.get("sourceFacts") or {})
        windows = facts.get("windows")
        if not isinstance(windows, list):
            raise ValueError("CI_ITEM_SNAPSHOT_INVALID")
        facts["windows"] = [
            {**window, "symptoms": sorted(symptoms_by_window.get(window.get("windowId"), set())),
             "evidenceIds": sorted(set(evidence_by_window.get(window.get("windowId"), [])))}
            if isinstance(window, dict) and window.get("windowId") in evidence_by_window
            else window for window in windows]
        snapshot["sourceFacts"] = facts
        snapshot["sourceFacts"]["autoLabelPolicyVersion"] = AUTO_LABEL_POLICY.version
        snapshot["evidence"] = list(snapshot.get("evidence") or []) + prepared["evidence"]
        snapshot["coverage"] = prepared["coverage"]
        dependency = {"sourceId": source["id"], "sourceVersion": source["sourceVersion"],
                      "sourceRevision": source["sourceRevision"]}
        assessment = {"billingOwnerId": context["billingOwnerId"],
            "sourceVersionId": source["sourceVersion"], "contextHash": context["contextHash"],
            "evaluatedContextVersion": context["contextVersion"],
            "questionVersion": prepared["questionVersion"],
            "extractorVersion": prepared["extractorVersion"], "model": DEFAULT_JEV_MODEL,
            "inputHash": prepared["inputHash"], "dependencies": [dependency],
            "bindings": {key: {**binding, "contextVersion": context["contextVersion"]}
                for key, binding in prepared["bindings"].items()},
            "answers": response["answers"], "usage": response["usage"]}
        return self.publish_validated_assessment(job_id=job_id, claim_token=claim_token,
            assessment=assessment, item_id=item["id"],
            expected_item_revision=item["revision"], sources=item["sources"],
            context_fences=item["contextFences"], snapshot=snapshot,
            observed_at=observed_at)

    def publish_claimed_pr_thread_response(
        self, *, job_id: str, claim_token: str, raw_body: bytes, observed_at: int,
    ) -> dict:
        """Publish one verified inline comment; thread reconciliation stays in the store."""
        prepared = self.prepare_claimed_analysis(
            job_id=job_id, claim_token=claim_token, now=observed_at)
        source, context = prepared["source"], prepared["context"]
        if source["sourceType"] != "pr_review_comment" or prepared["module"] != "pr":
            raise ValueError("CLAIMED_PR_THREAD_REQUIRED")
        self.store.require_claimed_provider_attempt(job_id=job_id, claim_token=claim_token,
            billing_owner_id=context["billingOwnerId"], input_key=prepared["inputHash"])
        response = validate_response(raw_body,
            expected_questions={key: tuple(question["criteria"])
                for key, question in prepared["questions"].items()},
            requested_model=DEFAULT_JEV_MODEL)
        dependency = {"sourceId": source["id"], "sourceVersion": source["sourceVersion"],
                      "sourceRevision": source["sourceRevision"]}
        fence = {"sourceId": source["id"], "contextId": context["id"],
                 "contextVersion": context["contextVersion"],
                 "configurationRevision": context["configurationRevision"],
                 "authorizationRevision": context["authorizationRevision"]}
        assessment = {"billingOwnerId": context["billingOwnerId"],
            "sourceVersionId": source["sourceVersion"], "contextHash": context["contextHash"],
            "evaluatedContextVersion": context["contextVersion"],
            "questionVersion": prepared["questionVersion"],
            "extractorVersion": prepared["extractorVersion"], "model": DEFAULT_JEV_MODEL,
            "inputHash": prepared["inputHash"],
            "dependencies": prepared["dependencies"],
            "bindings": {key: {**binding, "contextVersion": context["contextVersion"]}
                for key, binding in prepared["bindings"].items()},
            "answers": response["answers"], "usage": response["usage"]}
        evidence = [{**row, "segmentAnchor": row["anchor"], "status": "available"}
                    for row in prepared["evidence"]]
        snapshot = {"module": "pr", "actionTypes": [], "evidence": evidence,
                    "coverage": prepared["coverage"]}
        return self.publish_validated_assessment(job_id=job_id, claim_token=claim_token,
            assessment=assessment, item_id=None, expected_item_revision=None,
            sources=[dependency, *prepared["dependencies"]],
            context_fences=[fence, *prepared["dependencyFences"]], snapshot=snapshot,
            observed_at=observed_at)

    def publish_claimed_release_response(
        self, *, job_id: str, claim_token: str, raw_body: bytes, observed_at: int,
    ) -> dict:
        """Validate and publish a release answer against the current claimed facts."""
        prepared = self.prepare_claimed_analysis(
            job_id=job_id, claim_token=claim_token, now=observed_at)
        if prepared["source"]["sourceType"] != "release" or prepared["module"] != "updates":
            raise ValueError("CLAIMED_RELEASE_REQUIRED")
        self.store.require_claimed_provider_attempt(job_id=job_id, claim_token=claim_token,
            billing_owner_id=prepared["context"]["billingOwnerId"], input_key=prepared["inputHash"])
        response = validate_response(raw_body,
            expected_questions={key: tuple(question["criteria"])
                for key, question in prepared["questions"].items()},
            requested_model=DEFAULT_JEV_MODEL)
        source, context = prepared["source"], prepared["context"]
        dependency = {"sourceId": source["id"], "sourceVersion": source["sourceVersion"],
                      "sourceRevision": source["sourceRevision"]}
        fence = {"sourceId": source["id"], "contextId": context["id"],
                 "contextVersion": context["contextVersion"],
                 "configurationRevision": context["configurationRevision"],
                 "authorizationRevision": context["authorizationRevision"]}
        bindings = {key: {**binding, "contextVersion": context["contextVersion"]}
                    for key, binding in prepared["bindings"].items()}
        assessment = {"billingOwnerId": context["billingOwnerId"],
            "sourceVersionId": source["sourceVersion"], "contextHash": context["contextHash"],
            "evaluatedContextVersion": context["contextVersion"],
            "questionVersion": prepared["questionVersion"],
            "extractorVersion": prepared["extractorVersion"],
            "model": DEFAULT_JEV_MODEL, "inputHash": prepared["inputHash"],
            "dependencies": [dependency], "bindings": bindings,
            "answers": response["answers"], "usage": response["usage"]}
        snapshot = {"module": "updates", "actionTypes": [], "attentionState": "needs_confirmation",
            "lifecycle": "active", "evidence": prepared["evidence"],
            "coverage": prepared["coverage"]}
        return self.publish_validated_assessment(job_id=job_id, claim_token=claim_token,
            assessment=assessment, item_id=None, expected_item_revision=None,
            sources=[dependency], context_fences=[fence], snapshot=snapshot,
            observed_at=observed_at, project_update_item=True)

    def publish_claimed_pr_comment_response(
        self, *, job_id: str, claim_token: str, raw_body: bytes, observed_at: int,
    ) -> dict:
        """Publish a PR issue-comment answer and reconcile its follow-up Item."""
        prepared = self.prepare_claimed_analysis(
            job_id=job_id, claim_token=claim_token, now=observed_at)
        source, context = prepared["source"], prepared["context"]
        if source["sourceType"] != "pr_comment" or prepared["module"] != "pr":
            raise ValueError("CLAIMED_PR_COMMENT_REQUIRED")
        self.store.require_claimed_provider_attempt(job_id=job_id, claim_token=claim_token,
            billing_owner_id=context["billingOwnerId"], input_key=prepared["inputHash"])
        response = validate_response(raw_body,
            expected_questions={key: tuple(question["criteria"])
                for key, question in prepared["questions"].items()},
            requested_model=DEFAULT_JEV_MODEL)
        dependency = {"sourceId": source["id"], "sourceVersion": source["sourceVersion"],
                      "sourceRevision": source["sourceRevision"]}
        fence = {"sourceId": source["id"], "contextId": context["id"],
                 "contextVersion": context["contextVersion"],
                 "configurationRevision": context["configurationRevision"],
                 "authorizationRevision": context["authorizationRevision"]}
        assessment = {"billingOwnerId": context["billingOwnerId"],
            "sourceVersionId": source["sourceVersion"], "contextHash": context["contextHash"],
            "evaluatedContextVersion": context["contextVersion"],
            "questionVersion": prepared["questionVersion"],
            "extractorVersion": prepared["extractorVersion"], "model": DEFAULT_JEV_MODEL,
            "inputHash": prepared["inputHash"], "dependencies": [],
            "bindings": {key: {**binding, "contextVersion": context["contextVersion"]}
                for key, binding in prepared["bindings"].items()},
            "answers": response["answers"], "usage": response["usage"]}
        evidence = [{**row, "segmentAnchor": row["anchor"], "status": "available"}
                    for row in prepared["evidence"]]
        snapshot = {"module": "pr", "actionTypes": [], "evidence": evidence,
                    "coverage": prepared["coverage"]}
        return self.publish_validated_assessment(job_id=job_id, claim_token=claim_token,
            assessment=assessment, item_id=None, expected_item_revision=None,
            sources=[dependency], context_fences=[fence], snapshot=snapshot,
            observed_at=observed_at, project_pr_comment_item=True)

    def publish_claimed_pr_review_response(
        self, *, job_id: str, claim_token: str, raw_body: bytes, observed_at: int,
    ) -> dict:
        """Attach review-body semantics while retaining the formal GitHub action."""
        prepared = self.prepare_claimed_analysis(
            job_id=job_id, claim_token=claim_token, now=observed_at)
        source, context, item = prepared["source"], prepared["context"], prepared["item"]
        facts = source["sourceFacts"]
        if (source["sourceType"] != "pr_review_body" or prepared["module"] != "pr"
                or item is None or item["unitType"] != "pr_review_body"
                or facts.get("reviewState") != "CHANGES_REQUESTED"
                or facts.get("formalReviewStatus") != "effective"):
            raise ValueError("CLAIMED_FORMAL_REVIEW_REQUIRED")
        self.store.require_claimed_provider_attempt(job_id=job_id, claim_token=claim_token,
            billing_owner_id=context["billingOwnerId"], input_key=prepared["inputHash"])
        response = validate_response(raw_body,
            expected_questions={key: tuple(question["criteria"])
                for key, question in prepared["questions"].items()},
            requested_model=DEFAULT_JEV_MODEL)
        dependency = {"sourceId": source["id"], "sourceVersion": source["sourceVersion"],
                      "sourceRevision": source["sourceRevision"]}
        bindings = {key: {**binding, "contextVersion": context["contextVersion"]}
            for key, binding in prepared["bindings"].items()}
        assessment = {"billingOwnerId": context["billingOwnerId"],
            "sourceVersionId": source["sourceVersion"], "contextHash": context["contextHash"],
            "evaluatedContextVersion": context["contextVersion"],
            "questionVersion": prepared["questionVersion"],
            "extractorVersion": prepared["extractorVersion"], "model": DEFAULT_JEV_MODEL,
            "inputHash": prepared["inputHash"], "dependencies": [dependency],
            "bindings": bindings, "answers": response["answers"], "usage": response["usage"]}
        evidence = [{**row, "segmentAnchor": row["anchor"], "status": "available"}
                    for row in prepared["evidence"]]
        segments, known = _segments(assessment, evidence,
            {"id": source["id"], "sourceVersion": source["sourceVersion"]})
        semantic_actions = set()
        for values, rows in segments:
            labels = sorted(action for question, action in _ACTIONS.items()
                            if values.get(question) == "present")
            semantic_actions.update(labels)
            for row in rows:
                row["actionTypes"] = labels
                if values.get("completion_claim") == "present":
                    row["progressType"] = "completion_claim"
        snapshot = dict(item["snapshot"])
        if snapshot.get("module") != "pr" or "change_requested" not in snapshot.get("actionTypes", []):
            raise ValueError("FORMAL_REVIEW_ITEM_INVALID")
        rule_evidence_id = f"{source['id']}:body"
        rule_evidence = [row for row in snapshot.get("evidence", [])
            if row.get("id") == rule_evidence_id]
        snapshot["evidence"] = rule_evidence + evidence
        snapshot["actionTypes"] = sorted({"change_requested", *semantic_actions})
        snapshot["attentionState"] = "needs_action"
        snapshot["sourceFacts"] = {**snapshot.get("sourceFacts", {}),
            "bodySemanticComplete": bool(known and prepared["coverage"]["state"] == "complete")}
        snapshot["coverage"] = prepared["coverage"]
        return self.publish_validated_assessment(job_id=job_id, claim_token=claim_token,
            assessment=assessment, item_id=item["id"],
            expected_item_revision=item["revision"], sources=item["sources"],
            context_fences=item["contextFences"], snapshot=snapshot,
            observed_at=observed_at)

    def admit_claimed_provider_attempt(
        self, *, job_id: str, claim_token: str, input_key: str, now: int,
        owner_monthly_limit: int, global_monthly_limit: int,
        owner_rolling_limit: int, global_rolling_limit: int,
    ) -> dict:
        """Spend one attempt only while the claimed source and authority remain current."""
        job = self.store.get_background_job(job_id)
        if job is None:
            raise ValueError("JOB_CLAIM_LOST")
        return self.store.admit_provider_attempt(
            attempt_id=f"jev_{job_id}_{claim_token}",
            billing_owner_id=job["billingOwnerId"], input_key=input_key,
            occurred_at=now, owner_monthly_limit=owner_monthly_limit,
            global_monthly_limit=global_monthly_limit,
            owner_rolling_limit=owner_rolling_limit,
            global_rolling_limit=global_rolling_limit,
            job_id=job_id, claim_token=claim_token,
        )

    def publish_validated_assessment(
        self,
        *,
        job_id: str,
        claim_token: str,
        assessment: Mapping[str, object],
        item_id: str | None,
        expected_item_revision: int | None,
        sources: Sequence[Mapping[str, object]],
        context_fences: Sequence[Mapping[str, object]],
        snapshot: Mapping[str, object],
        observed_at: int,
        project_update_item: bool = False,
        project_pr_comment_item: bool = False,
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
            project_update_item=project_update_item,
            project_pr_comment_item=project_pr_comment_item,
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
