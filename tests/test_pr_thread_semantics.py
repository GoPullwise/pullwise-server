"""Synthetic typed answers exercise aggregation, not model quality."""
import time
from pathlib import Path

import pytest

from pullwise_server.jev_questions import pr_questions
from pullwise_server.product_store import ProductStore


class ThreadFixture:
    def __init__(self, path):
        self.store = ProductStore(path)
        self.store.initialize()
        self.now = int(time.time())
        self.records = {}

    def source(self, cid, body, *, reply=None, thread="thread1", complete=True, lifecycle="active", **facts):
        record = self.store.upsert_source_snapshot(
            source_id=cid, source_type="pr_review_comment", external_key=f"comment:{cid}",
            repository_id="repo", content={"body": body},
            source_facts={**dict(commentId=cid, pullNumber=42, threadId=thread,
                associationVerified=thread is not None, inReplyToId=reply,
                threadCoverage="complete" if complete else "partial", isResolved=False,
                threadCommentIds=list(dict.fromkeys([*self.records, cid])),
                isOutdated=False, author={"githubId": "reviewer"},
                pullAuthor={"githubId": "author"}), **facts},
            source_url="https://github.com/a/b/pull/42", processing_mode="model",
            completeness="complete" if complete else "partial", lifecycle=lifecycle, observed_at=self.now)
        self.records[cid] = record
        self.store.set_source_context(source_id=cid, context_id="repo:repo:pr", context_version=1,
            configuration_revision=1, authorization_revision=1, authorization_valid_until=self.now + 300,
            accessible=True, billing_owner_id="owner", analysis_enabled=True)
        return record

    def publish(self, cid, *present, dependencies=(), uncertain=()):
        record = self.records[cid]
        refs = [dict(sourceId=sid, sourceVersion=self.records[sid]["sourceVersion"],
                     sourceRevision=self.records[sid]["sourceRevision"]) for sid in (cid, *dependencies)]
        evidence_id = cid + ":s0"
        answers = {}
        bindings = {}
        for qid, question in pr_questions(1).items():
            name = qid[3:]
            choice = "unclear" if name in uncertain else "present" if name in present else (
                "not_stated" if name == "blocking_language" else "absent")
            answers[qid] = dict(type="choice", choice=choice, confidence=0.95,
                probabilities={key: float(key == choice) for key in question["criteria"]})
            bindings[qid] = dict(sourceId=cid, sourceVersion=record["sourceVersion"],
                                 segmentAnchor="s0", evidenceIds=[evidence_id])
        reservation = self.store.reserve_processing_unit(charge_key=f"{cid}:{record['sourceRevision']}",
            billing_owner_id="owner", period="period", module="pr", limit=100)
        content = next(s for s in self.store.list_sources_for_billing_owner("owner", include_content=True) if s["id"] == cid)
        return self.store.publish_assessment_result(reservation_id=reservation["reservationId"],
            item_id=None, expected_item_revision=None, sources=refs,
            context_fences=[dict(sourceId=r["sourceId"], contextId="repo:repo:pr", contextVersion=1,
                configurationRevision=1, authorizationRevision=1) for r in refs],
            assessment=dict(billingOwnerId="owner", sourceVersionId=record["sourceVersion"],
                contextHash="pr", evaluatedContextVersion=1, questionVersion="pr-followup/v3",
                extractorVersion="pr-segments/v1", model="jev-1.13.0", inputHash=str(refs),
                dependencies=refs[1:], bindings=bindings, answers=answers, usage={}),
            snapshot=dict(module="pr", evidence=[dict(id=evidence_id, sourceId=cid,
                sourceVersion=record["sourceVersion"], segmentAnchor="s0", status="available",
                text=content["content"]["body"])]), observed_at=self.now)

    def items(self):
        return self.store.list_items_for_billing_owner("owner", include_history=True)


def test_verified_thread_has_one_multilabel_item_and_source_based_usage(tmp_path):
    f = ThreadFixture(tmp_path / "product.db")
    f.source("1", "Please add tests.")
    f.source("2", "Why this approach?", reply="1")
    f.publish("1", "change_request")
    f.publish("2", "question", dependencies=("1",))
    assert len(f.items()) == 1
    item = f.items()[0]
    assert item["unit"] == {"type": "pr_thread", "externalId": "thread1"}
    assert set(item["actionTypes"]) == {"change_requested", "reply_needed"}
    assert {r["sourceId"] for r in item["sources"]} == {"1", "2"}
    assert len(item["assessments"]) == 2
    assert item["nextActors"] == [{"kind": "user", "githubId": "author"}]
    assert f.store.processing_usage(billing_owner_id="owner", period="period")["used"] == 2


def test_completion_claim_and_thanks_preserve_adjacent_handling_without_reminder(tmp_path):
    f = ThreadFixture(tmp_path / "product.db")
    f.source("1", "Please add tests.")
    f.publish("1", "change_request")
    before = f.items()[0]
    f.store.patch_item_handling(item_id=before["id"], item_version=before["itemVersion"],
        expected_revision=before["revision"], actor_id="owner", disposition="done")
    f.now += 1
    f.source("2", "Done, thanks.", reply="1")
    f.publish("2", "completion_claim", dependencies=("1",))
    after = f.items()[0]
    assert after["id"] == before["id"]
    assert after["handling"]["disposition"] == "done"
    assert after["handling"]["carriedFromItemVersion"] == before["itemVersion"]
    assert after["attentionUpdatedAt"] == before["attentionUpdatedAt"]
    assert "completionClaims" not in after["sourceFacts"]
    assert next(e for e in after["evidence"] if e["sourceId"] == "2")["progressType"] == "completion_claim"
    assert after["lifecycle"] == "active"


@pytest.mark.parametrize("thread,reply,positive", [(None, None, "change_request"),
    ("thread1", "missing", "completion_claim"), ("thread1", None, "completion_claim")])
def test_unknown_identity_or_progress_alone_never_creates_item(tmp_path, thread, reply, positive):
    f = ThreadFixture(tmp_path / "product.db")
    f.source("1", "Text", thread=thread, reply=reply)
    f.publish("1", positive)
    assert f.items() == []


def test_partial_or_unknown_result_cannot_close_existing_request(tmp_path):
    f = ThreadFixture(tmp_path / "product.db")
    f.source("1", "Please add tests.")
    f.publish("1", "change_request")
    before = f.items()[0]
    f.now += 1
    f.source("1", "Maybe no need.", complete=False)
    f.publish("1", uncertain=("change_request",))
    after = f.items()[0]
    assert after["attentionState"] == "needs_confirmation"
    assert after["lifecycle"] == "active"
    assert after["attentionUpdatedAt"] == before["attentionUpdatedAt"]


def test_parent_edit_invalidates_child_without_changing_child_body(tmp_path):
    from pullwise_server.pr_followup import reconcile_thread
    f = ThreadFixture(tmp_path / "product.db")
    f.source("1", "Context A")
    f.source("2", "Please change this.", reply="1")
    f.publish("1")
    f.publish("2", "change_request", dependencies=("1",))
    before = f.items()[0]
    f.source("1", "Context B")
    with f.store.atomic() as store:
        reconcile_thread(store, source_id="1", context_id="repo:repo:pr", now=f.now)
    after = f.items()[0]
    assert after["itemVersion"] > before["itemVersion"]
    assert after["attentionState"] == "needs_confirmation"
    assert after["assessments"] == []
    assert any(e["status"] == "expired" for e in after["evidence"])


def test_new_request_and_aba_never_restore_old_done(tmp_path):
    f = ThreadFixture(tmp_path / "product.db")
    f.source("1", "A")
    f.publish("1", "change_request")
    before = f.items()[0]
    f.store.patch_item_handling(item_id=before["id"], item_version=before["itemVersion"],
        expected_revision=before["revision"], actor_id="owner", disposition="done")
    versions = [before["itemVersion"]]
    for text in ("B", "A"):
        f.now += 1
        f.source("1", text)
        f.publish("1", "change_request")
        item = f.items()[0]
        versions.append(item["itemVersion"])
        assert item["handling"]["disposition"] == "open"
    assert versions == sorted(set(versions))


def test_cross_thread_reply_cannot_route_or_borrow_parent_assessment(tmp_path):
    f = ThreadFixture(tmp_path / "product.db")
    f.source("1", "Request", thread="other")
    f.source("2", "Review again", reply="1")
    f.publish("1", "change_request")
    f.publish("2", "rereview_request", dependencies=("1",))
    assert len(f.items()) == 1
    assert f.items()[0]["unit"]["externalId"] == "other"


def test_author_reply_uses_verified_parent_identity_and_claim_does_not_handoff(tmp_path):
    f = ThreadFixture(tmp_path / "product.db")
    f.source("1", "Please change.")
    f.source("2", "Done, please review again.", reply="1", author={"githubId": "author"})
    f.publish("1", "change_request")
    f.publish("2", "rereview_request", "completion_claim", dependencies=("1",))
    item = f.items()[0]
    # Keep both unresolved requests; no inferred temporal handoff or technical fix.
    assert item["nextActors"] == [{"kind": "user", "githubId": "author"}, {"kind": "user", "githubId": "reviewer"}]
    assert item["lifecycle"] == "active"


def test_fact_replay_does_not_version_or_charge_and_parent_edit_marks_pending(tmp_path):
    from pullwise_server.product_rule_items import publish_rule_items
    f = ThreadFixture(tmp_path / "product.db")
    f.source("1", "Context A")
    f.source("2", "Please change.", reply="1")
    f.publish("1")
    f.publish("2", "change_request", dependencies=("1",))
    before = f.items()[0]
    usage = f.store.processing_usage(billing_owner_id="owner", period="period")
    def sync():
        publish_rule_items(f.store, source={"sourceType": "pr_review_comment"}, record=f.records["1"],
            target={"context_id": "repo:repo:pr"}, now=f.now)
    sync()
    assert f.items()[0]["itemVersion"] == before["itemVersion"]
    f.source("1", "Context B")
    sync()
    assert f.items()[0]["attentionState"] == "needs_confirmation"
    assert f.store.processing_usage(billing_owner_id="owner", period="period") == usage
    assert f.store.count_jobs(job_type="analyze_source") == 0


def test_claim_publication_uses_current_thread_not_a_precomputed_item_snapshot(tmp_path):
    from pullwise_server.product_jobs import ProductJobScheduler, TrustedTrigger
    f = ThreadFixture(tmp_path / "product.db")
    f.source("1", "Change")
    f.source("2", "Question", reply="1")
    f.publish("1", "change_request")
    reservation = f.store.reserve_processing_unit(charge_key="second", billing_owner_id="owner",
        period="period", module="pr", limit=100)
    job = ProductJobScheduler(f.store).schedule_analysis(source_context_key="second", source_id="2",
        context_id="repo:repo:pr", reservation_id=reservation["reservationId"], trigger=TrustedTrigger.SCHEDULED_DISCOVERY)
    claim = f.store.claim_next_analysis_job(now=f.now)
    original = f.store.publish_assessment_result
    def claimed(**kwargs):
        kwargs.update(reservation_id=reservation["reservationId"], job_id=job["id"], claim_token=claim["claimToken"])
        return original(**kwargs)
    f.store.publish_assessment_result = claimed
    f.publish("2", "question", dependencies=("1",))
    assert f.store.get_background_job(job["id"])["status"] == "succeeded"
    assert set(f.items()[0]["actionTypes"]) == {"change_requested", "reply_needed"}


def test_lost_thread_proof_keeps_existing_item_pending(tmp_path):
    from pullwise_server.product_rule_items import publish_rule_items
    f = ThreadFixture(tmp_path / "product.db")
    f.source("1", "Change")
    f.publish("1", "change_request")
    f.source("1", "Edited without thread proof", thread=None)
    publish_rule_items(f.store, source={"sourceType": "pr_review_comment"}, record=f.records["1"],
        target={"context_id": "repo:repo:pr"}, now=f.now)
    assert f.items()[0]["attentionState"] == "needs_confirmation"
    assert not f.items()[0]["assessments"]
    assert f.items()[0]["sourceFacts"]["threadResolved"] is None


def test_pr_closed_child_fact_closes_thread_without_parent_source(tmp_path):
    from pullwise_server.product_rule_items import publish_rule_items
    f = ThreadFixture(tmp_path / "product.db")
    f.source("1", "Change")
    f.publish("1", "change_request")
    f.source("1", "Change", pullState="closed")
    publish_rule_items(f.store, source={"sourceType": "pr_review_comment"}, record=f.records["1"],
        target={"context_id": "repo:repo:pr"}, now=f.now)
    assert f.items()[0]["closureReason"] == "source_closed"


def test_unfetched_roster_member_prevents_negative_closure(tmp_path):
    f = ThreadFixture(tmp_path / "product.db")
    f.source("1", "Change")
    f.publish("1", "change_request")
    f.source("1", "No request", threadCommentIds=["1", "unfetched"])
    f.publish("1")
    assert f.items()[0]["attentionState"] == "needs_confirmation"


def test_parent_dependency_cannot_be_declared_without_frozen_publication_fence(tmp_path):
    f = ThreadFixture(tmp_path / "product.db")
    f.source("1", "Parent")
    f.source("2", "Request", reply="1")
    publish = f.store.publish_assessment_result
    def unbound(**kwargs):
        kwargs["sources"] = [s for s in kwargs["sources"] if s["sourceId"] != "1"]
        kwargs["context_fences"] = [s for s in kwargs["context_fences"] if s["sourceId"] != "1"]
        return publish(**kwargs)
    f.store.publish_assessment_result = unbound
    with pytest.raises(ValueError, match="ASSESSMENT_DEPENDENCY_BINDING_MISMATCH"):
        f.publish("2", "change_request", dependencies=("1",))
    assert f.store.processing_usage(billing_owner_id="owner", period="period")["used"] == 0


def test_unchanged_action_excerpt_in_complete_edit_can_carry_handling(tmp_path):
    f = ThreadFixture(tmp_path / "product.db")
    f.source("1", "Change\n\nThanks")
    publish = f.store.publish_assessment_result
    def excerpt(**kwargs):
        kwargs["snapshot"]["evidence"][0]["text"] = "Change"
        return publish(**kwargs)
    f.store.publish_assessment_result = excerpt
    f.publish("1", "change_request")
    old = f.items()[0]
    f.store.patch_item_handling(item_id=old["id"], item_version=old["itemVersion"],
        expected_revision=old["revision"], actor_id="owner", disposition="done")
    f.source("1", "Change\n\nThanks again")
    f.publish("1", "change_request")
    assert f.items()[0]["handling"]["carriedFromItemVersion"] == old["itemVersion"]


def test_deletion_only_removes_its_source_and_entire_deleted_thread_keeps_reason(tmp_path):
    from pullwise_server.pr_followup import reconcile_thread
    f = ThreadFixture(tmp_path / "product.db")
    f.source("1", "Change")
    f.source("2", "Question")
    f.publish("1", "change_request")
    f.publish("2", "question")
    for sid in ("1", "2"):
        f.source(sid, "", lifecycle="source_deleted")
        with f.store.atomic() as store:
            reconcile_thread(store, source_id=sid, context_id="repo:repo:pr", now=f.now)
        item = f.items()[0]
        if sid == "1":
            assert item["actionTypes"] == ["reply_needed"]
            assert item["lifecycle"] == "active"
        else:
            assert item["closureReason"] == "source_deleted"
