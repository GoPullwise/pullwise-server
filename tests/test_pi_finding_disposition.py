import pytest

from pullwise_server import app


@pytest.mark.parametrize("disposition,expected", [
    ("UNVALIDATED", "potential_risk"),
    ("plausible", "potential_risk"),
    ("VALIDATED", "static_proof"),
])
def test_pi_disposition_survives_public_issue_projection(disposition, expected):
    job = {"job_id": "job_pi", "scan_id": "scan_pi", "user_id": "usr_pi",
           "repo": "acme/api", "commit": "a" * 40}
    finding = {"issue_id": "candidate", "title": "Candidate", "severity": "high",
               "category": "correctness", "confidence": 0.95,
               "validation_status": disposition,
               "location": {"file": "app.js", "line": 1},
               "description": "Possible failure", "impact": "Requests fail",
               "remediation": "Validate first",
               "evidence": [{"path": "app.js", "line": 1, "summary": "broken();"}]}
    issue = app.worker_protocol_findings(job, {"summary": {"top_findings": [finding]}})[0]
    assert app.issue_payload(issue)["verificationStatus"] == expected


def test_unvalidated_disposition_cannot_be_overridden_by_a_proof_label():
    source = app.worker_protocol_finding_source({
        "validation_status": "UNVALIDATED", "verificationStatus": "verified",
        "location": {"file": "app.js", "line": 1},
    })
    assert source["verificationStatus"] == "potential_risk"
