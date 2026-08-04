"""Read query for the single release-evaluator store."""

from __future__ import annotations


LOAD_RELEASE_EVALUATION_SQL = """
SELECT
    benchmark.document_bytes AS benchmark_bytes,
    benchmark.document_sha256 AS benchmark_document_sha256,
    benchmark.size_bytes AS benchmark_size_bytes,
    benchmark.bundle_digest AS stored_benchmark_digest,
    benchmark.benchmark_id AS stored_benchmark_id,
    benchmark.package_identity AS benchmark_package_identity,
    benchmark.package_version AS benchmark_package_version,
    benchmark.package_content_sha256 AS benchmark_package_content_sha256,
    benchmark.package_root_sha256 AS benchmark_package_root_sha256,
    policy.document_bytes AS policy_bytes,
    policy.document_sha256 AS policy_document_sha256,
    policy.size_bytes AS policy_size_bytes,
    policy.policy_digest AS stored_policy_digest,
    policy.policy_id AS stored_policy_id,
    policy.benchmark_digest AS policy_benchmark_digest,
    policy.benchmark_ref_sha256,
    policy.benchmark_ref_size_bytes,
    policy.package_identity AS policy_package_identity,
    policy.package_version AS policy_package_version,
    policy.package_content_sha256 AS policy_package_content_sha256,
    policy.package_root_sha256 AS policy_package_root_sha256,
    sample_set.document_bytes AS sample_set_bytes,
    sample_set.document_sha256 AS sample_set_document_sha256,
    sample_set.size_bytes AS sample_set_size_bytes,
    sample_set.sample_set_digest AS stored_sample_set_digest,
    sample_set.sample_set_id AS stored_sample_set_id,
    sample_set.benchmark_digest AS sample_set_benchmark_digest,
    sample_set.policy_digest AS sample_set_policy_digest,
    sample_set.benchmark_ref_sha256 AS sample_set_benchmark_ref_sha256,
    sample_set.benchmark_ref_size_bytes AS sample_set_benchmark_ref_size_bytes,
    sample_set.policy_ref_sha256 AS sample_set_policy_ref_sha256,
    sample_set.policy_ref_size_bytes AS sample_set_policy_ref_size_bytes,
    sample_set.package_identity AS sample_set_package_identity,
    sample_set.package_version AS sample_set_package_version,
    sample_set.package_content_sha256 AS sample_set_package_content_sha256,
    sample_set.package_root_sha256 AS sample_set_package_root_sha256,
    report.document_bytes AS report_bytes,
    report.document_sha256 AS report_document_sha256,
    report.size_bytes AS report_size_bytes,
    report.report_digest AS stored_report_digest,
    report.report_id AS stored_report_id,
    report.benchmark_digest AS report_benchmark_digest,
    report.policy_digest AS report_policy_digest,
    report.sample_set_digest AS report_sample_set_digest,
    report.benchmark_ref_sha256 AS report_benchmark_ref_sha256,
    report.benchmark_ref_size_bytes AS report_benchmark_ref_size_bytes,
    report.policy_ref_sha256,
    report.policy_ref_size_bytes,
    report.sample_set_ref_sha256,
    report.sample_set_ref_size_bytes,
    report.verdict,
    report.exit_code,
    report.package_identity AS report_package_identity,
    report.package_version AS report_package_version,
    report.package_content_sha256 AS report_package_content_sha256,
    report.package_root_sha256 AS report_package_root_sha256
FROM agent_current_release_gate_reports AS report
JOIN agent_current_release_gate_sample_sets AS sample_set
    ON sample_set.sample_set_digest = report.sample_set_digest
JOIN agent_current_release_gate_policies AS policy
    ON policy.policy_digest = report.policy_digest
JOIN agent_current_release_benchmark_bundles AS benchmark
    ON benchmark.bundle_digest = report.benchmark_digest
WHERE report.report_id = ?
"""


__all__ = ["LOAD_RELEASE_EVALUATION_SQL"]
