"""Source results must survive independently of actionable Items."""
import tempfile
import time
from contextlib import closing
from pathlib import Path

import pytest

from pullwise_server.product_store import ProductStore
from pullwise_server.product_jobs import ProductJobScheduler, ProductJobExecutor, TrustedTrigger
from test_saved_updates_projection import assessment as updates_assessment


@pytest.fixture
def publication():
    with tempfile.TemporaryDirectory() as directory:
        store = ProductStore(Path(directory) / "product.sqlite3")
        store.initialize()
        now = int(time.time())
        source = store.upsert_source_snapshot(
            source_id="release", source_type="release", external_key="release:1",
            repository_id="upstream", content={"body": "Documentation update."},
            source_facts={"releaseId": "1"}, source_url="https://github.com/a/b/releases/1",
            processing_mode="model", completeness="complete", lifecycle="active", observed_at=now,
        )
        watch = store.create_watch(owner_id="owner", target_repository_id=None,
            upstream_repository_id="github:101", billing_owner_id="owner",
            interests=["documentation"], enabled=True, analysis_enabled=True)
        context = dict(source_id="release", context_id="watch:1", context_version=1,
                       configuration_revision=1, authorization_revision=1,
                       authorization_valid_until=now + 300, accessible=True,
                       billing_owner_id="owner", watch_id=watch["id"], analysis_enabled=True)
        store.set_source_context(**context)
        reservation = store.reserve_processing_unit(
            charge_key="release:1", billing_owner_id="owner", period="period", module="updates", limit=10)
        args = dict(
            reservation_id=reservation["reservationId"], item_id=None, expected_item_revision=None,
            assessment=dict(billingOwnerId="owner", sourceVersionId=source["sourceVersion"],
                            contextHash="context", evaluatedContextVersion=1,
                            questionVersion="updates-filter/v3", extractorVersion="updates-units/v1",
                            model="jev-1.13.0", inputHash="input", dependencies=[],
                            bindings={"u0_relevance": {"changeUnitId": "u0", "evidenceIds": ["e1"]}},
                            answers={"u0_relevance": {"choice": "not_relevant"}}, usage={}),
            sources=[dict(sourceId="release", sourceVersion=source["sourceVersion"], sourceRevision=1)],
            context_fences=[dict(sourceId="release", contextId="watch:1", contextVersion=1,
                                configurationRevision=1, authorizationRevision=1)],
            snapshot={"module": "updates", "evidence": [{"id": "e1", "text": "Documentation update."}]},
            observed_at=now,
        )
        yield store, args, context


def test_no_item_result_is_atomic_durable_and_detail_only(publication):
    store, args, _ = publication
    published = store.publish_assessment_result(**args)
    assert published["item"] is None
    resumed = ProductStore(store.database_path)
    detail = resumed.list_sources_for_billing_owner("owner", include_content=True)[0]
    context = detail["contexts"][0]
    assert context["itemId"] is None
    assert context["processingStatus"] == "assessed"
    assert context["assessments"] == [published["assessment"]]
    assert context["evidence"] == args["snapshot"]["evidence"]
    assert "assessments" not in resumed.list_sources_for_billing_owner("owner")[0]["contexts"][0]
    assert resumed.list_items_for_billing_owner("owner") == []
    assert resumed.processing_usage(billing_owner_id="owner", period="period")["used"] == 1


@pytest.mark.parametrize("change,error", [
    ({"configuration_revision": 2}, "STALE_CONTEXT"),
    ({"accessible": False}, "STALE_AUTHORIZATION"),
    ({"analysis_enabled": False}, "ANALYSIS_DISABLED"),
    ({"billing_owner_id": "other"}, "RESERVATION_OWNER_MISMATCH"),
])
def test_source_publication_fences_roll_back(publication, change, error):
    store, args, context = publication
    store.set_source_context(**{**context, **change})
    with pytest.raises(ValueError, match=error):
        store.publish_assessment_result(**args)
    assert store.processing_usage(billing_owner_id="owner", period="period")["reserved"] == 1
    with closing(store.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM assessments").fetchone()[0] == 0


@pytest.mark.parametrize("change", [{"context_version": 2}, {"context_stale": True}])
def test_old_context_never_exposes_current_assessment(publication, change):
    store, args, context = publication
    store.publish_assessment_result(**args)
    store.set_source_context(**{**context, **change})
    detail = store.list_sources_for_billing_owner("owner", include_content=True)[0]
    assert detail["contexts"][0]["assessments"] == []


def test_source_revision_invalidates_result_but_analysis_disable_does_not(publication):
    store, args, context = publication
    published = store.publish_assessment_result(**args)
    store.set_source_context(**{**context, "analysis_enabled": False, "configuration_revision": 2})
    detail = store.list_sources_for_billing_owner("owner", include_content=True)[0]
    assert detail["contexts"][0]["assessments"] == [published["assessment"]]
    store.set_source_revision("release", 2)
    detail = store.list_sources_for_billing_owner("owner", include_content=True)[0]
    assert detail["contexts"][0]["assessments"] == []
    assert detail["contexts"][0]["evidence"] == []


def test_missing_dependency_fence_cannot_publish(publication):
    store, args, _ = publication
    args["context_fences"] = []
    with pytest.raises(ValueError, match="ASSESSMENT_CONTEXT_BINDING_MISMATCH"):
        store.publish_assessment_result(**args)
    assert store.processing_usage(billing_owner_id="owner", period="period")["used"] == 0


def test_source_list_projects_saved_answers_without_item_labels(publication):
    store, args, context = publication
    coverage = {"state": "partial", "selectedUnits": 1, "rawSourcePartial": False}
    store.set_source_context(**context, coverage=coverage)
    saved = updates_assessment([("not_relevant", "absent", "absent", "absent", "absent")])
    for binding in saved["bindings"].values():
        binding["evidenceIds"] = ["e1"]
    args["assessment"].update(saved)
    store.publish_assessment_result(**args)
    result = store.list_sources_for_billing_owner("owner")[0]["contexts"][0]
    assert result["relevance"] == "unclear"
    assert all(value == "unclear" for value in result["updateSignals"].values())
    assert result["units"][0]["evidenceIds"] == ["e1"]
    assert "assessments" not in result
    store.set_source_context(**context, coverage={**coverage, "state": "complete"})
    refreshed = store.list_sources_for_billing_owner("owner")[0]["contexts"][0]
    assert refreshed["relevance"] == "unclear"
    assert refreshed["coverage"]["state"] == "partial"


def test_claimed_source_only_job_completes_once_and_replay_does_not_charge(publication):
    store, args, _ = publication
    job = ProductJobScheduler(store).schedule_analysis(
        source_context_key="watch:1:release", source_id="release", context_id="watch:1",
        reservation_id=args["reservation_id"], trigger=TrustedTrigger.SCHEDULED_DISCOVERY)
    executor = ProductJobExecutor(store)
    claim = executor.claim_next_analysis(now=args["observed_at"])
    assert claim["id"] == job["id"]
    publish_args = {key: value for key, value in args.items() if key != "reservation_id"}
    result = executor.publish_validated_assessment(
        **publish_args, job_id=job["id"], claim_token=claim["claimToken"])
    assert result["item"] is None
    assert store.get_background_job(job["id"])["status"] == "succeeded"
    with pytest.raises(ValueError, match="JOB_CLAIM_LOST"):
        ProductJobExecutor(ProductStore(store.database_path)).publish_validated_assessment(
            **publish_args, job_id=job["id"], claim_token=claim["claimToken"])
    assert store.processing_usage(billing_owner_id="owner", period="period")["used"] == 1
