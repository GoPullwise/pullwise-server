"""Trusted fact-sync orchestration. Readers are server-owned read-only GitHub adapters.

No model transport is imported here. The reader and permission-proof refresher
must be assembled by the server, never supplied by a request. This offline seam
does not claim live GitHub adapter/permission coverage.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping

from . import product_store
from .github_transport import GitHubUnavailable
from .product_jobs import ProductJobScheduler, TrustedTrigger
from .product_store import ProductStore
from .product_rule_items import publish_rule_items, reconcile_pr_items


@dataclass(frozen=True)
class FactPage:
    sources: tuple[Mapping[str, object], ...]
    next_cursor: str | None
    high_watermark: str
    run_states: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class DiscoveryAuthorizationProof:
    """Result of a fresh server-owned check of the entire supplied target.

    The checker must verify the module, installation, repository and billing
    owner's continuing authority (plus target-repository authority for shared
    watches). Public visibility alone is insufficient. It must use bounded
    network I/O, return no credentials, and raise on an inconclusive check.
    """
    accessible: bool
    observed_at: int
    valid_until: int


_EVENTS = {
    "release": ("updates", "release", {"published", "edited", "deleted", "unpublished", "prereleased", "released"}),
    "issue_comment": ("pr", "comment", {"created", "edited", "deleted"}),
    "pull_request_review": ("pr", "review", {"submitted", "edited", "dismissed"}),
    "pull_request_review_comment": ("pr", "comment", {"created", "edited", "deleted"}),
    "pull_request": ("pr", "pull_request", {"opened", "synchronize", "reopened", "closed", "review_requested", "review_request_removed"}),
    "pull_request_review_thread": ("pr", "thread", {"resolved", "unresolved"}),
    "workflow_run": ("ci", "workflow_run", {"requested", "in_progress", "completed"}),
    "workflow_job": ("ci", "workflow_job", {"queued", "in_progress", "completed"}),
}
_TYPES = {"updates": {"release"}, "pr": {"pr_state", "pr_comment", "pr_review_body", "pr_review_comment"}, "ci": {"ci_failure"}}
_INTERVAL = {"pr": 900, "ci": 900, "updates": 21600}
_BACKFILL = {"pr": 20, "ci": 5, "updates": 1}


def _retry_time(error: Exception, now: int) -> int:
    retry_at = error.retry_at if isinstance(error, GitHubUnavailable) else None
    if type(retry_at) is not int or not 0 <= retry_at <= 253402300799:
        retry_at = 0
    return max(now + 60, retry_at)


def _timestamp(source: Mapping[str, object]) -> int | None:
    facts = source["sourceFacts"]
    fields = {"release": ("updatedAt", "publishedAt"), "ci_failure": ("completedAt",),
              "pr_state": ("updatedAt", "createdAt"),
              "pr_review_body": ("updatedAt", "submittedAt"),
              "pr_comment": ("updatedAt", "createdAt"),
              "pr_review_comment": ("updatedAt", "createdAt")}.get(source["sourceType"], ())
    for name in fields:
        value = facts.get(name)
        if value is None:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            return int(parsed.timestamp())
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None
    return None


def _material_ready(source: Mapping[str, object], target: dict) -> bool:
    if source["lifecycle"] != "active" or source["processingMode"] != "model" or source["completeness"] == "unavailable":
        return False
    if source["sourceType"] == "release" and source["sourceFacts"].get("prerelease") and not target["include_prerelease"]:
        return False
    if target["module"] == "ci":
        return any(str(window.get("text") or "").strip() for window in source["content"].get("windows", []))
    return bool(str(source["content"].get("body") or "").strip())


class GitHubWebhookReceiver:
    def __init__(self, store: ProductStore, *, app_id: str, webhook_secret: str):
        self.store = store
        self.app_id = app_id
        self.webhook_secret = webhook_secret

    @staticmethod
    def _authorized(target: dict | None, now: int) -> bool:
        return target is not None and target["enabled"] and bool(target["accessible"]) and target["valid_until"] > now

    def receive_github_event(self, raw: bytes, *, signature: str, event: str, delivery_id: str, now: int) -> dict:
        if len(raw) > 1024 * 1024:
            raise ValueError("GITHUB_BODY_TOO_LARGE")
        expected = "sha256=" + hmac.new(self.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
        if (not self.webhook_secret or not isinstance(signature, str) or not signature.isascii()
                or not hmac.compare_digest(expected, signature)):
            raise ValueError("GITHUB_SIGNATURE_INVALID")
        if not isinstance(delivery_id, str) or not 1 <= len(delivery_id) <= 128:
            raise ValueError("GITHUB_DELIVERY_INVALID")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("GITHUB_PAYLOAD_INVALID")
        if ((event == "installation" and payload.get("action") in {"deleted", "suspend"})
                or (event == "installation_repositories" and payload.get("action") == "removed")):
            return self._receive_revocation(payload, event=event, delivery_id=delivery_id,
                                            digest=hashlib.sha256(raw).hexdigest(), now=now)
        spec = _EVENTS.get(event)
        if spec is None or payload.get("action") not in spec[2]:
            return {"accepted": 0}
        for field in ("repository", "installation", spec[1]):
            if not isinstance(payload.get(field), dict):
                raise ValueError("GITHUB_BINDING_INVALID")
        if event == "issue_comment" and (not isinstance(payload.get("issue"), dict) or not isinstance(payload["issue"].get("pull_request"), dict)):
            return {"accepted": 0}
        repository = payload.get("repository", {}).get("id")
        installation = payload.get("installation", {}).get("id")
        resource = payload.get(spec[1], {}).get("id")
        if any(type(value) is not int or value < 1 for value in (repository, installation, resource)):
            raise ValueError("GITHUB_BINDING_INVALID")
        pull_number = None
        if spec[0] == "pr":
            parent = payload.get("issue" if event == "issue_comment" else "pull_request")
            pull_number = parent.get("number") if isinstance(parent, dict) else None
            if type(pull_number) is not int or not 1 <= pull_number <= 2**31 - 1:
                raise ValueError("GITHUB_BINDING_INVALID")
        digest = hashlib.sha256(raw).hexdigest()
        with self.store.atomic() as store, store._immediate() as connection:
            receipt = connection.execute("SELECT * FROM github_deliveries WHERE app_id=? AND delivery_id=?",
                                         (self.app_id, delivery_id)).fetchone()
            if receipt is not None:
                if receipt["payload_hash"] != digest or receipt["event"] != event:
                    raise ValueError("GITHUB_DELIVERY_CONFLICT")
                return {"accepted": 0}
            rows = connection.execute(
                "SELECT control_key FROM discovery_targets WHERE app_id=? AND installation_id=? AND github_repository_id=? AND module=?",
                (self.app_id, str(installation), str(repository), spec[0]),
            ).fetchall()
            targets = [store.discovery_target(row["control_key"]) for row in rows]
            targets = [target for target in targets if self._authorized(target, now)]
            if not targets:
                return {"accepted": 0}
            connection.execute("INSERT INTO github_deliveries VALUES (?, ?, ?, ?, ?)",
                               (self.app_id, delivery_id, event, digest, now))
            for target in targets:
                # A receipt stores identifiers only; payload text never becomes authoritative facts.
                connection.execute(
                    """INSERT INTO github_delivery_targets(app_id, delivery_id, control_key, repository_id,
                           installation_id, event, resource_id, pull_number, ready_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (self.app_id, delivery_id, target["control_key"], str(repository), str(installation), event,
                     str(resource), pull_number, now + 30),
                )
        return {"accepted": len(targets)}

    def _receive_revocation(self, payload: dict, *, event: str, delivery_id: str, digest: str, now: int) -> dict:
        installation = payload.get("installation")
        if not isinstance(installation, dict) or type(installation.get("id")) is not int or installation["id"] < 1:
            raise ValueError("GITHUB_BINDING_INVALID")
        repositories = None
        if event == "installation_repositories":
            removed = payload.get("repositories_removed")
            if not isinstance(removed, list) or any(not isinstance(repo, dict) or type(repo.get("id")) is not int or repo["id"] < 1 for repo in removed):
                raise ValueError("GITHUB_BINDING_INVALID")
            repositories = [str(repo["id"]) for repo in removed]
        with self.store.atomic() as store, store._immediate() as connection:
            receipt = connection.execute("SELECT * FROM github_deliveries WHERE app_id=? AND delivery_id=?", (self.app_id, delivery_id)).fetchone()
            if receipt:
                if receipt["payload_hash"] != digest or receipt["event"] != event:
                    raise ValueError("GITHUB_DELIVERY_CONFLICT")
                return {"accepted": 0}
            count = store.revoke_discovery_installation(app_id=self.app_id, installation_id=str(installation["id"]),
                                                       repository_ids=repositories, observed_at=now)
            if count:
                connection.execute("INSERT INTO github_deliveries VALUES (?, ?, ?, ?, ?)",
                                   (self.app_id, delivery_id, event, digest, now))
            return {"accepted": count}


class ProductFactSync(GitHubWebhookReceiver):
    def __init__(self, store: ProductStore, *, read_page: Callable, processing_budget: Callable,
                 app_id: str, webhook_secret: str, global_active_limit: int = 1000,
                 owner_active_limit: int = 100, refresh_authorization: Callable | None = None):
        super().__init__(store, app_id=app_id, webhook_secret=webhook_secret)
        self.read_page = read_page
        self.processing_budget = processing_budget
        self.global_active_limit = global_active_limit
        self.owner_active_limit = owner_active_limit
        self.refresh_authorization = refresh_authorization

    def run_forever(self, stop_event) -> None:
        """One server-owned fact worker; never run on an HTTP request thread."""
        while not stop_event.is_set():
            for tick in (self.run_authorization_due, self.run_events, self.run_due):
                if stop_event.is_set():
                    break
                try:
                    tick(now=product_store._now())
                except Exception as error:
                    # Adapter exceptions may contain private response bodies.
                    logging.getLogger(__name__).warning("Fact sync tick failed (%s)", type(error).__name__)
            stop_event.wait(1)

    def run_authorization_due(self, *, now: int, limit: int = 100) -> list[dict]:
        """Renew read authority independently of discovery; never admit model work."""
        if self.refresh_authorization is None:
            return []
        with self.store._read() as connection:
            rows = connection.execute(
                """SELECT t.control_key FROM discovery_targets t
                   LEFT JOIN discovery_authorization_refreshes r USING(control_key)
                   WHERE t.valid_until<=? AND COALESCE(r.next_attempt_at,0)<=?
                     AND COALESCE(r.claimed_until,0)<=?
                   ORDER BY COALESCE(r.next_attempt_at,0), t.control_key LIMIT ?""",
                (now + 60, now, now, min(max(limit, 1), 100)),
            ).fetchall()
        return [self._refresh_authorization(row["control_key"], now=max(now, product_store._now()))
                for row in rows]

    def _refresh_authorization(self, control_key: str, *, now: int) -> dict:
        token = uuid.uuid4().hex
        with self.store.atomic() as store, store._immediate() as connection:
            target = store.discovery_target(control_key)
            connection.execute("INSERT OR IGNORE INTO discovery_authorization_refreshes(control_key) VALUES (?)", (control_key,))
            claim = connection.execute(
                """UPDATE discovery_authorization_refreshes SET claim_token=?, claimed_until=?, next_attempt_at=?
                   WHERE control_key=? AND claimed_until<=? AND next_attempt_at<=?""",
                (token, now + 120, now + 60, control_key, now, now),
            )
            if claim.rowcount != 1:
                return {"status": "not_due"}
            if target is None or not target["enabled"] or target["valid_until"] > now + 60:
                connection.execute("UPDATE discovery_authorization_refreshes SET claim_token=NULL, claimed_until=0 WHERE control_key=?", (control_key,))
                return {"status": "not_due"}
        try:
            # No SQLite write lock is held during the external permission check.
            proof = self.refresh_authorization(target=dict(target), now=now)
            completed_at = max(now, product_store._now())
            if (not isinstance(proof, DiscoveryAuthorizationProof)
                    or type(proof.accessible) is not bool
                    or type(proof.observed_at) is not int or type(proof.valid_until) is not int
                    or not now <= proof.observed_at <= completed_at
                    or not proof.observed_at <= proof.valid_until <= proof.observed_at + 300
                    or (proof.accessible and proof.valid_until <= completed_at)):
                raise ValueError("INVALID_AUTHORIZATION_PROOF")
            with self.store.atomic() as store, store._immediate() as connection:
                claim = connection.execute("SELECT * FROM discovery_authorization_refreshes WHERE control_key=?", (control_key,)).fetchone()
                current = store.discovery_target(control_key)
                if claim["claim_token"] != token or claim["claimed_until"] <= completed_at:
                    return {"status": "superseded"}
                # Includes auth revision/expiry, config epoch, resource identity
                # and billing owner. A late check cannot undo a webhook revoke.
                if current != target:
                    connection.execute("UPDATE discovery_authorization_refreshes SET claim_token=NULL, claimed_until=0 WHERE control_key=?", (control_key,))
                    return {"status": "superseded"}
                store.set_discovery_authorization(
                    resource_kind=target["resource_kind"], resource_id=target["resource_id"], module=target["module"],
                    github_repository_id=target["github_repository_id"], installation_id=target["installation_id"],
                    app_id=target["app_id"], authorization_revision=target["authorization_revision"] + (bool(target["accessible"]) != proof.accessible),
                    accessible=proof.accessible, valid_until=proof.valid_until if proof.accessible else proof.observed_at,
                    observed_at=proof.observed_at)
                connection.execute(
                    """UPDATE discovery_authorization_refreshes SET claim_token=NULL, claimed_until=0, next_attempt_at=?
                       WHERE control_key=?""",
                    (max(completed_at + 60, proof.valid_until - 60) if proof.accessible else completed_at + 60, control_key),
                )
            return {"status": "renewed" if proof.accessible else "denied"}
        except Exception as error:
            # Never log checker exception messages: upstream errors can include secrets.
            logging.getLogger(__name__).warning("Authorization refresh failed (%s)", type(error).__name__)
            with self.store._immediate() as connection:
                connection.execute(
                    """UPDATE discovery_authorization_refreshes SET claim_token=NULL, claimed_until=0, next_attempt_at=?
                       WHERE control_key=? AND claim_token=?""",
                    (_retry_time(error, max(now, product_store._now())), control_key, token),
                )
            return {"status": "unavailable"}

    def run_events(self, *, now: int, limit: int = 100) -> list[dict]:
        with self.store._read() as connection:
            rows = connection.execute(
                """SELECT * FROM github_delivery_targets WHERE state='pending' AND ready_at<=?
                   ORDER BY ready_at, rowid LIMIT ?""", (now, min(max(limit, 1), 100)),
            ).fetchall()
        results = []
        for row in rows:
            try:
                result = self._sync(row["control_key"], now=now, trigger=TrustedTrigger.GITHUB_EVENT, event=dict(row))
            except Exception as error:
                with self.store._immediate() as connection:
                    connection.execute("UPDATE github_delivery_targets SET ready_at=? WHERE app_id=? AND delivery_id=? AND control_key=?",
                                       (_retry_time(error, max(now, product_store._now())), row["app_id"], row["delivery_id"], row["control_key"]))
                result = {"status": "unavailable"}
            results.append(result)
            if result["status"] in {"completed", "unauthorized"}:
                with self.store._immediate() as connection:
                    connection.execute("UPDATE github_delivery_targets SET state='completed' WHERE app_id=? AND delivery_id=? AND control_key=?",
                                       (row["app_id"], row["delivery_id"], row["control_key"]))
        return results

    def run_due(self, *, now: int, limit: int = 100) -> list[dict]:
        """Normal server clock entrypoint, independent of GET and manual sync."""
        with self.store._read() as connection:
            keys = connection.execute("SELECT control_key FROM discovery_targets WHERE next_scheduled_at<=? ORDER BY next_scheduled_at, control_key LIMIT ?",
                                      (now, min(max(limit, 1), 100))).fetchall()
        results = []
        for row in keys:
            try:
                results.append(self.run_scheduled(row["control_key"], now=now))
            except Exception as error:
                logging.getLogger(__name__).warning("Fact sync failed (%s)", type(error).__name__)
                results.append({"status": "unavailable"})
        return results

    def run_scheduled(self, control_key: str, *, now: int) -> dict:
        return self._sync(control_key, now=now, trigger=TrustedTrigger.SCHEDULED_DISCOVERY)

    def run_manual(self, control_key: str, *, now: int) -> dict:
        return self._sync(control_key, now=now, trigger=None)

    def _sync(self, control_key: str, *, now: int, trigger: TrustedTrigger | None, event: dict | None = None) -> dict:
        with self.store.atomic() as store, store._immediate() as connection:
            target = store.discovery_target(control_key)
            if not self._authorized(target, now):
                return {"status": "unauthorized"}
            backoff = connection.execute("SELECT retry_at FROM github_read_backoffs WHERE control_key=?", (control_key,)).fetchone()
            if backoff is not None and backoff[0] > now:
                return {"status": "throttled"}
            connection.execute("UPDATE discovery_targets SET configuration_stamp=?, configuration_epoch=? WHERE control_key=?",
                               (target["configuration_stamp"], target["configuration_revision"], control_key))
            if event is not None and (event["installation_id"] != target["installation_id"] or event["app_id"] != target["app_id"]):
                return {"status": "unauthorized"}
            if trigger == TrustedTrigger.SCHEDULED_DISCOVERY:
                if target["next_scheduled_at"] > now:
                    return {"status": "not_due"}
                connection.execute("UPDATE discovery_targets SET next_scheduled_at=? WHERE control_key=?",
                                   (now + _INTERVAL[target["module"]], control_key))
            # Credential resolution fences the complete supplied snapshot. Use
            # the state after this transaction's own schedule/stamp writes.
            target = store.discovery_target(control_key)
            checkpoint = None
            if trigger is not None:
                checkpoint = dict(connection.execute("SELECT * FROM processing_controls WHERE control_key=?", (control_key,)).fetchone())
            # Shared across manual/event/schedule and all watches reading the same upstream.
            parent = f"{target['repository_id']}:{target['module']}"
            connection.execute("INSERT INTO fact_sync_generations VALUES (?, 1) ON CONFLICT(parent_key) DO UPDATE SET generation=generation+1", (parent,))
            generation = connection.execute("SELECT generation FROM fact_sync_generations WHERE parent_key=?", (parent,)).fetchone()[0]
        try:
            page = self.read_page(target=target, cursor=checkpoint["discovery_cursor"] if checkpoint and event is None else None,
                                  high_watermark=checkpoint["high_watermark"] if checkpoint and event is None else None,
                                  event=event)
        except GitHubUnavailable as error:
            with self.store._immediate() as connection:
                connection.execute(
                    """INSERT INTO github_read_backoffs(control_key,retry_at) VALUES (?,?)
                       ON CONFLICT(control_key) DO UPDATE SET retry_at=MAX(retry_at,excluded.retry_at)""",
                    (control_key, _retry_time(error, max(now, product_store._now()))),
                )
            raise
        if not isinstance(page, FactPage) or len(page.sources) > 100:
            raise ValueError("INVALID_FACT_PAGE")
        if any(not isinstance(source, Mapping) or not isinstance(source.get("sourceId"), str) for source in page.sources):
            raise ValueError("INVALID_FACT_PAGE")
        if len({source["sourceId"] for source in page.sources}) != len(page.sources):
            raise ValueError("INVALID_FACT_PAGE")
        now = max(now, product_store._now())
        persisted = []
        with self.store.atomic() as store, store._immediate() as connection:
            current = store.discovery_target(control_key)
            if not self._authorized(current, now) or self._fence(current) != self._fence(target):
                return {"status": "unauthorized"}
            if connection.execute("SELECT generation FROM fact_sync_generations WHERE parent_key=?", (parent,)).fetchone()[0] != generation:
                return {"status": "superseded"}
            self._persist_run_states(connection, page.run_states, target=target, now=now)
            for source in page.sources:
                if source["repositoryId"] != target["repository_id"] or source["sourceType"] not in _TYPES[target["module"]]:
                    raise ValueError("SOURCE_BINDING_MISMATCH")
                changed_at = _timestamp(source)
                previous = connection.execute("SELECT authoritative_changed_at FROM source_observations WHERE source_id=?", (source["sourceId"],)).fetchone()
                if previous and previous[0] is not None and (changed_at is None or changed_at < previous[0]):
                    continue
                prior_version = connection.execute("SELECT latest_version FROM source_records WHERE source_id=?", (source["sourceId"],)).fetchone()
                record = store.upsert_source_snapshot(source_id=source["sourceId"], source_type=source["sourceType"],
                    external_key=source["externalKey"], repository_id=source["repositoryId"], content=source["content"],
                    source_facts=source["sourceFacts"], source_url=source["sourceUrl"], processing_mode=source["processingMode"],
                    completeness=source["completeness"], lifecycle=source["lifecycle"], observed_at=now)
                connection.execute("INSERT INTO source_observations VALUES (?, ?) ON CONFLICT(source_id) DO UPDATE SET authoritative_changed_at=excluded.authoritative_changed_at",
                                   (source["sourceId"], changed_at))
                old_context = connection.execute("SELECT * FROM source_contexts WHERE source_id=? AND context_id=?",
                                                 (record["id"], target["context_id"])).fetchone()
                status = old_context["processing_status"] if old_context else "pending"
                if prior_version is not None and prior_version[0] != record["sourceVersion"]:
                    status = "pending"
                if not target["analysis_enabled"]:
                    status = "analysis_disabled"
                elif source["processingMode"] == "rules_only":
                    status = "rules_only"
                elif not _material_ready(source, target):
                    status = "needs_manual"
                store.set_source_context(source_id=record["id"], context_id=target["context_id"],
                    context_version=target["context_version"], configuration_revision=target["configuration_revision"],
                    authorization_revision=target["authorization_revision"], authorization_valid_until=target["valid_until"],
                    accessible=True, billing_owner_id=target["billing_owner_id"],
                    watch_id=target["resource_id"] if target["resource_kind"] == "watch" else None,
                    item_id=old_context["item_id"] if old_context else None, processing_status=status,
                    analysis_enabled=target["analysis_enabled"], context_stale=bool(old_context and old_context["context_stale"]),
                    coverage=json.loads(old_context["coverage_json"]) if old_context else {})
                persisted.append((source, record, changed_at))
                publish_rule_items(store, source=source, record=record, target=target, now=now)
            reconcile_pr_items(store, target=target, now=now)
        # Facts are committed before eligibility, quotas, checkpoints or job admission.
        if trigger is None:
            return {"status": "completed", "sources": len(persisted)}
        period, processing_limit = self.processing_budget(target["billing_owner_id"], now)
        now = max(now, product_store._now())
        with self.store.atomic() as store, store._immediate() as connection:
            current = store.discovery_target(control_key)
            if not self._authorized(current, now) or self._fence(current) != self._fence(target):
                return {"status": "unauthorized"}
            if connection.execute("SELECT generation FROM fact_sync_generations WHERE parent_key=?", (parent,)).fetchone()[0] != generation:
                return {"status": "superseded"}
            if trigger == TrustedTrigger.SCHEDULED_DISCOVERY:
                try:
                    store.advance_discovery_checkpoint(control_key, expected_cursor=checkpoint["discovery_cursor"],
                        expected_high_watermark=checkpoint["high_watermark"], next_cursor=page.next_cursor,
                        next_high_watermark=page.high_watermark, observed_at=now)
                except ValueError as error:
                    if str(error) != "DISCOVERY_CHECKPOINT_MISMATCH":
                        raise
                    return {"status": "checkpoint_conflict"}
            if not target["analysis_enabled"]:
                return {"status": "completed", "sources": len(persisted)}
            store.establish_processing_eligibility(control_key, eligible_since=target["analysis_authorized_at"] if target["analysis_authorized_at"] is not None else now)
            if trigger == TrustedTrigger.SCHEDULED_DISCOVERY:
                candidates = sorted((entry for entry in persisted if entry[2] is not None and now - 30 * 86400 <= entry[2] <= now and _material_ready(entry[0], target)),
                                    key=lambda entry: (-entry[2], entry[0]["externalKey"]))
                store.freeze_initial_backfill(control_key, source_keys=[entry[0]["externalKey"] for entry in candidates], limit=_BACKFILL[target["module"]])
            for source, record, changed_at in persisted:
                if not _material_ready(source, target):
                    continue
                eligible = changed_at is not None and changed_at <= now and store.discovery_source_eligible(
                    control_key, source_key=source["externalKey"], authoritative_changed_at=changed_at)["eligible"]
                if not eligible:
                    self._status(connection, record["id"], target["context_id"], "not_scheduled")
                    continue
                if event is not None and changed_at > now - 30:
                    self._status(connection, record["id"], target["context_id"], "pending")
                    return {"status": "stabilizing"}
                self._admit(store, connection, target, source, record, trigger, period, processing_limit, now)
        return {"status": "completed", "sources": len(persisted)}

    @staticmethod
    def _persist_run_states(connection, states, *, target: dict, now: int) -> None:
        """Keep native CI attempts separate from failure sources and recovery claims.

        Each snapshot contains the currently observed jobs page. Partial pages
        never become a complete run merely because a last page was observed.
        Caller holds the same permission/config/generation-fenced fact transaction.
        """
        if not isinstance(states, tuple) or len(states) > 100 or (states and target["module"] != "ci"):
            raise ValueError("INVALID_CI_RUN_STATES")
        keys = set()
        fields = {"repositoryId", "runId", "runAttempt", "workflowId", "headSha", "status", "conclusion",
                  "updatedAt", "pullRequests", "jobs", "coverage"}
        for state in states:
            if not isinstance(state, Mapping) or set(state) != fields:
                raise ValueError("INVALID_CI_RUN_STATE")
            run_id, attempt = state["runId"], state["runAttempt"]
            if (state["repositoryId"] != target["repository_id"] or not isinstance(run_id, str)
                    or not run_id.isascii() or not run_id.isdigit() or not 0 < len(run_id) <= 20
                    or int(run_id) < 1 or type(attempt) is not int or not 1 <= attempt <= 2**31 - 1
                    or (run_id, attempt) in keys):
                raise ValueError("INVALID_CI_RUN_BINDING")
            keys.add((run_id, attempt))
            try:
                parsed = datetime.fromisoformat(state["updatedAt"].replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
                updated_at = int(parsed.timestamp())
            except (AttributeError, TypeError, ValueError, OverflowError):
                raise ValueError("INVALID_CI_RUN_TIME") from None
            coverage = state["coverage"]
            if (not isinstance(state["jobs"], list) or len(state["jobs"]) > 100
                    or not isinstance(state["pullRequests"], list) or len(state["pullRequests"]) > 100
                    or not isinstance(coverage, Mapping) or type(coverage.get("jobsComplete")) is not bool
                    or type(coverage.get("jobsPage")) is not int or coverage["jobsPage"] < 1
                    or (coverage["jobsComplete"] and (coverage["jobsPage"] != 1 or coverage.get("nextJobsPage") is not None))):
                raise ValueError("INVALID_CI_RUN_COVERAGE")
            snapshot = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            if len(snapshot.encode()) > 1024 * 1024:
                raise ValueError("INVALID_CI_RUN_SIZE")
            previous = connection.execute(
                "SELECT snapshot_json FROM github_run_states WHERE repository_id=? AND run_id=? AND run_attempt=?",
                (target["repository_id"], run_id, attempt),
            ).fetchone()
            if (previous is not None and json.loads(previous[0])["status"] == "completed"
                    and state["status"] != "completed"):
                # A rerun has a new attempt. Same-second or stale upstream
                # observations cannot turn this terminal attempt back into work.
                continue
            connection.execute(
                """INSERT INTO github_run_states(repository_id,run_id,run_attempt,authoritative_updated_at,snapshot_json,observed_at)
                   VALUES (?,?,?,?,?,?) ON CONFLICT(repository_id,run_id,run_attempt) DO UPDATE SET
                       authoritative_updated_at=excluded.authoritative_updated_at,
                       snapshot_json=excluded.snapshot_json,observed_at=excluded.observed_at
                   WHERE excluded.authoritative_updated_at>=github_run_states.authoritative_updated_at""",
                (target["repository_id"], run_id, attempt, updated_at, snapshot, now),
            )

    @staticmethod
    def _fence(target: dict) -> tuple:
        return tuple(target[key] for key in ("resource_id", "configuration_revision", "context_version", "authorization_revision", "billing_owner_id", "analysis_enabled"))

    @staticmethod
    def _status(connection, source_id: str, context_id: str, status: str):
        connection.execute("UPDATE source_contexts SET processing_status=? WHERE source_id=? AND context_id=?", (status, source_id, context_id))

    def _admit(self, store, connection, target, source, record, trigger, period, limit, now):
        owner, context = target["billing_owner_id"], target["context_id"]
        logical_key = f"{record['id']}:{context}"
        latest = connection.execute("SELECT * FROM background_jobs WHERE logical_key=? ORDER BY generation DESC LIMIT 1",
                                    (f"analyze_source:{logical_key}",)).fetchone()
        if latest and latest["source_version_id"] == record["sourceVersion"] and latest["context_version"] != target["context_version"]:
            # This is a context refresh, not a new source. The separate bounded
            # cooldown path must authorize it; discovery cannot relabel it as new.
            connection.execute("UPDATE source_contexts SET context_stale=1, processing_status='not_scheduled' WHERE source_id=? AND context_id=?",
                               (record["id"], context))
            return
        if latest and latest["source_version_id"] == record["sourceVersion"] and latest["context_version"] == target["context_version"]:
            same_fences = (latest["source_revision"] == record["sourceRevision"]
                           and latest["configuration_revision"] == target["configuration_revision"]
                           and latest["authorization_revision"] == target["authorization_revision"])
            if latest["state"] in {"succeeded", "failed"} or (same_fences and latest["state"] in {"queued", "running", "retry_wait"}):
                status = {"queued": "pending", "running": "processing", "retry_wait": "pending", "succeeded": "assessed", "failed": "failed"}[latest["state"]]
                self._status(connection, record["id"], context, status)
                return
        attempts = connection.execute(
            """SELECT COALESCE(MAX(attempt), 0) FROM background_jobs WHERE logical_key=?
               AND source_version_id=? AND context_version=?""",
            (f"analyze_source:{logical_key}", record["sourceVersion"], target["context_version"]),
        ).fetchone()[0]
        if latest and latest["state"] in {"queued", "running", "retry_wait"}:
            old = connection.execute("SELECT * FROM processing_usage_ledger WHERE reservation_id=?", (latest["reservation_id"],)).fetchone()
            if old:
                store._release_processing_reservation(connection, old, now=now)
            connection.execute("UPDATE background_jobs SET state='superseded', claim_token=NULL WHERE id=?", (latest["id"],))
        if attempts >= 3:
            self._status(connection, record["id"], context, "failed")
            return
        charge_identity = [owner, source["sourceType"], source["sourceId"], record["sourceVersion"]]
        if target["module"] == "updates":
            charge_identity = [owner, context, source["sourceId"], record["sourceVersion"]]
        elif target["module"] == "ci":
            charge_identity = [owner, source["repositoryId"], *[source["sourceFacts"][name] for name in ("runId", "runAttempt", "jobId")]]
        charge_key = "processing:" + hashlib.sha256(json.dumps(charge_identity, separators=(",", ":")).encode()).hexdigest()
        ledger = connection.execute("SELECT * FROM processing_usage_ledger WHERE charge_key=?", (charge_key,)).fetchone()
        if ledger and ledger["state"] == "consumed":
            # Context refresh/cache replay is separate from new-source discovery.
            self._status(connection, record["id"], context, "not_scheduled")
            return
        try:
            reservation = store.reserve_processing_unit(charge_key=charge_key, billing_owner_id=owner,
                period=ledger["period"] if ledger and ledger["state"] == "reserved" else period,
                module=target["module"], limit=limit)
        except ValueError as error:
            if str(error) != "PROCESSING_QUOTA":
                raise
            self._status(connection, record["id"], context, "paused_quota")
            return
        try:
            job = ProductJobScheduler(store).schedule_analysis(source_context_key=logical_key, source_id=record["id"],
                context_id=context, reservation_id=reservation["reservationId"], trigger=trigger,
                global_active_limit=self.global_active_limit, owner_active_limit=self.owner_active_limit)
        except ValueError as error:
            if str(error) not in {"ANALYSIS_QUEUE_GLOBAL_LIMIT", "ANALYSIS_QUEUE_OWNER_LIMIT"}:
                raise
        else:
            if not job["reused"]:
                connection.execute("UPDATE background_jobs SET attempt=? WHERE id=?", (attempts, job["id"]))
            self._status(connection, record["id"], context, "failed" if job["status"] == "failed" else "pending")
