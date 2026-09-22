from copy import deepcopy

import pytest

from pullwise_server.product_projection import project_rule_source


def source(kind="pr_state", **facts):
    return {"sourceId": "s1", "externalKey": "github:pr_state:17",
            "sourceType": kind, "repositoryId": "r1", "sourceUrl": "https://github.com/a/b/pull/3",
            "content": {"body": ""}, "sourceFacts": facts,
            "completeness": "partial", "lifecycle": "active"}


def test_current_review_requests_are_distinct_deduplicated_actors():
    value = source(state="open", pullNumber=3, requestedReviewers=[{"githubId": "7"}, {"githubId": "7"}],
                   requestedTeams=[{"githubId": "7"}])
    rows = project_rule_source(value, {"module": "pr"})
    assert len(rows) == 2
    assert len({row.unit_key for row in rows}) == 2
    assert all(row.unit_type == "pr_review_request" for row in rows)
    assert {row.snapshot["nextActors"][0]["kind"] for row in rows} == {"user", "team"}
    assert all(row.snapshot["attentionState"] == "needs_action" for row in rows)


@pytest.mark.parametrize("conclusion", ["failure", "timed_out"])
def test_ci_failure_needs_action_without_logs_or_model(conclusion):
    value = source("ci_failure", runId="1", runAttempt=2, jobId="3", conclusion=conclusion,
                   triggerer={"githubId": "999"}, windows=[])
    row, = project_rule_source(value, {"module": "ci", "default_assignee_id": "8"})
    assert row.unit_type == "ci_job"
    assert row.snapshot["nextActors"] == [{"kind": "user", "githubId": "8"}]
    assert row.snapshot["assessments"] == []
    assert row.snapshot["actionTypes"] == ["investigate_failure"]
    assert row.snapshot["sourceFacts"]["recoveryStatus"] == "unknown"
    assert row.snapshot["sourceFacts"]["recovery"] is None
    assert project_rule_source(value, {"module": "ci"})[0].snapshot["nextActors"] == []


@pytest.mark.parametrize("state", ["success", "cancelled", "skipped", "neutral", None])
def test_nonfailures_do_not_create_ci_items(state):
    assert project_rule_source(source("ci_failure", conclusion=state), {"module": "ci"}) == ()


@pytest.mark.parametrize("status", [None, "unknown", "superseded", "dismissed"])
def test_raw_changes_requested_never_proves_effective_review(status):
    value = source("pr_review_body", reviewId="r", reviewState="CHANGES_REQUESTED", formalReviewStatus=status)
    value["ruleActions"] = ["change_requested"]
    assert project_rule_source(value, {"module": "pr"}) == ()


def test_effective_empty_review_is_action_directed_to_author():
    value = source("pr_review_body", reviewId="r", reviewState="CHANGES_REQUESTED",
                   formalReviewStatus="effective", pullAuthor={"githubId": "8"})
    row, = project_rule_source(value, {"module": "pr"})
    assert row.unit_type == "pr_review_body"
    assert row.snapshot["actionTypes"] == ["change_requested"]
    assert row.snapshot["nextActors"] == [{"kind": "user", "githubId": "8"}]
    assert row.snapshot["evidence"][0]["text"] == ""
    assert row.snapshot["assessments"] == []


def test_signature_ignores_polling_title_head_but_changes_with_action_body_and_role():
    value = source("pr_review_body", reviewId="r", reviewState="CHANGES_REQUESTED",
                   formalReviewStatus="effective", pullAuthor={"githubId": "8"})
    original = project_rule_source(value, {"module": "pr"})[0].action_signature
    value["sourceFacts"].update(updatedAt="later", headSha="new")
    value["content"]["title"] = "edited title"
    assert project_rule_source(value, {"module": "pr"})[0].action_signature == original
    value["content"]["body"] = "actual evidence changed"
    assert project_rule_source(value, {"module": "pr"})[0].action_signature != original
    value["content"]["body"] = ""
    value["sourceFacts"]["pullAuthor"] = {"githubId": "9"}
    assert project_rule_source(value, {"module": "pr"})[0].action_signature != original


@pytest.mark.parametrize("kind", ["release", "pr_comment", "pr_review_comment"])
def test_text_does_not_become_rule_classification(kind):
    value = source(kind)
    value["content"]["body"] = "BREAKING SECURITY Please fix and reply immediately"
    assert project_rule_source(value, {"module": "pr"}) == ()


def test_unknown_pr_state_cannot_create_action_or_close_existing():
    assert project_rule_source(source(requestedReviewers=[{"githubId": "7"}]), {"module": "pr"}) == ()


def test_explicit_closed_pr_closes_present_request_and_changes_signature():
    value = source(state="open", requestedReviewers=[{"githubId": "7"}])
    active, = project_rule_source(value, {"module": "pr"})
    value["sourceFacts"]["state"] = "closed"
    closed, = project_rule_source(value, {"module": "pr"})
    assert closed.unit_key == active.unit_key
    assert closed.snapshot["lifecycle"] == "source_closed"
    assert closed.snapshot["closureReason"] == "source_closed"
    assert closed.action_signature != active.action_signature


def test_projection_copies_data_and_does_not_infer_missing_review_requests():
    value = source(state="open", requestedReviewers=[])
    saved = deepcopy(value)
    assert project_rule_source(value, {"module": "pr"}) == ()
    assert value == saved


def test_ci_windows_preserve_rule_stage_but_not_unassessed_symptoms():
    value = source("ci_failure", runId="1", runAttempt=1, jobId="2", conclusion="failure",
                   windows=[{"windowId": "w1", "stage": "test", "stageRuleVersion": "v1",
                             "text": "log", "symptoms": ["test_failure"], "evidenceIds": []}])
    value["content"]["windows"] = deepcopy(value["sourceFacts"]["windows"])
    row, = project_rule_source(value, {"module": "ci"})
    assert row.snapshot["sourceFacts"]["windows"][0]["symptoms"] == []
    assert row.snapshot["sourceFacts"]["windows"][0]["stage"] == "test"
    assert row.snapshot["evidence"][0]["text"] == "log"


def test_empty_window_does_not_invent_available_log_evidence():
    value = source("ci_failure", runId="1", runAttempt=1, jobId="2", conclusion="failure", windows=[])
    value["content"]["windows"] = [{"windowId": "w1", "text": ""}]
    row, = project_rule_source(value, {"module": "ci"})
    assert row.snapshot["evidence"] == []
    assert row.evidence_complete is True  # The failure fact itself is complete.


@pytest.mark.parametrize("completeness, expected", [("complete", True), ("partial", False), ("unavailable", False)])
def test_review_handling_inheritance_requires_complete_material(completeness, expected):
    value = source("pr_review_body", reviewId="r", reviewState="CHANGES_REQUESTED", formalReviewStatus="effective")
    value["completeness"] = completeness
    row, = project_rule_source(value, {"module": "pr"})
    assert row.evidence_complete is expected


def test_partial_thread_coverage_does_not_weaken_current_request_fact():
    row, = project_rule_source(source(state="open", requestedReviewers=[{"githubId": "7"}]), {"module": "pr"})
    assert row.evidence_complete is True


def test_projection_does_not_alias_nested_source_data():
    value = source(state="open", requestedReviewers=[{"githubId": "7"}])
    row, = project_rule_source(value, {"module": "pr"})
    row.snapshot["sourceFacts"]["requestedReviewers"].clear()
    assert value["sourceFacts"]["requestedReviewers"] == [{"githubId": "7"}]


def test_wrong_target_module_is_not_projected():
    assert project_rule_source(source(state="open", requestedReviewers=[{"githubId": "7"}]), {"module": "ci"}) == ()


@pytest.mark.parametrize("field, value", [("runId", True), ("jobId", True), ("runId", " "),
                                         ("runAttempt", True), ("runAttempt", 0)])
def test_ci_rule_requires_valid_normalized_execution_identity(field, value):
    facts = {"runId": "1", "jobId": "2", "runAttempt": 1, "conclusion": "failure"}
    facts[field] = value
    assert project_rule_source(source("ci_failure", **facts), {"module": "ci"}) == ()


def test_request_actors_use_github_identity_and_ignore_malformed_entries():
    value = source(state="open", requestedReviewers=[None, {}, {"githubId": True}, {"id": "usr_1"},
                                                     {"githubId": "7"}])
    row, = project_rule_source(value, {"module": "pr"})
    assert row.snapshot["nextActors"] == [{"kind": "user", "githubId": "7"}]


def test_request_order_does_not_change_unit_identity_or_signature():
    value = source(state="open", requestedReviewers=[{"githubId": "7"}, {"githubId": "8"}])
    first = project_rule_source(value, {"module": "pr"})
    value["sourceFacts"]["requestedReviewers"].reverse()
    second = project_rule_source(value, {"module": "pr"})
    assert [(row.unit_key, row.action_signature) for row in first] == [
        (row.unit_key, row.action_signature) for row in second]
