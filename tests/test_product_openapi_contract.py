from __future__ import annotations

import unittest
from pathlib import Path


CONTRACT = Path(__file__).parents[1] / "openapi" / "product-v1.yaml"


class ProductOpenApiContractTest(unittest.TestCase):
    def test_p1_contract_exposes_shared_lists_handling_sync_and_usage(self) -> None:
        text = CONTRACT.read_text(encoding="utf-8")
        for path in (
            "/api/v1/me:",
            "/api/v1/items/overview:",
            "/api/v1/sources:",
            "/api/v1/sources/{sourceId}:",
            "/api/v1/items:",
            "/api/v1/items/{itemId}:",
            "/api/v1/repositories/{repositoryId}/service:",
            "/api/v1/repositories/{repositoryId}/sync:",
            "/api/v1/watches/{watchId}/sync:",
            "/api/v1/usage:",
            "/api/v1/usage/events:",
            "/api/v1/jobs/{jobId}:",
        ):
            self.assertIn(path, text)
        self.assertIn("cookieSession: []", text)
        self.assertIn("apiKey: []", text)

    def test_contract_has_no_user_model_submission_or_p5b_fake_paths(self) -> None:
        text = CONTRACT.read_text(encoding="utf-8").lower()
        for forbidden in (
            "/batches",
            "/classify",
            "/reanalyze",
            "/retry-analysis",
            "forceanalysis",
            "/visualizations",
            "/timeline",
        ):
            self.assertNotIn(forbidden, text)

    def test_source_and_item_state_enums_preserve_pending_partial_and_unknown(self) -> None:
        text = CONTRACT.read_text(encoding="utf-8")
        self.assertRegex(text, r"processingStatus:\s*\n\s+type: string\s*\n\s+enum:.*pending")
        self.assertIn("needs_manual", text)
        self.assertIn("not_scheduled", text)
        self.assertIn("state: { type: string, enum: [complete, partial, unavailable] }", text)
        self.assertIn("recoveryStatus: { type: string, enum: [unknown, not_observed, verified] }", text)
        self.assertIn("additionalProperties: false", text)

    def test_contract_declares_only_current_product_scopes(self) -> None:
        text = CONTRACT.read_text(encoding="utf-8")
        for scope in (
            "profile:read",
            "repositories:read",
            "repositories:manage",
            "items:read",
            "items:write",
            "sync:write",
            "watches:read",
            "watches:write",
            "usage:read",
        ):
            self.assertIn(scope, text)
        self.assertNotIn("scans:write", text)
        self.assertNotIn("processing:write", text)

    def test_sync_operations_explicitly_state_that_they_never_schedule_models(self) -> None:
        text = CONTRACT.read_text(encoding="utf-8")
        for operation in ("syncRepository", "syncWatch"):
            start = text.index(f"operationId: {operation}")
            description = text[start : start + 350]
            self.assertIn("never schedules model work", description)

    def test_item_assessment_contract_includes_question_and_evidence_bindings(self) -> None:
        text = CONTRACT.read_text(encoding="utf-8")
        self.assertIn("AssessmentBinding:", text)
        self.assertIn("Assessment:", text)
        self.assertIn("publishContextVersion:", text)
        self.assertIn("bindings:", text)
        self.assertIn("evidenceIds:", text)
        self.assertIn(
            "assessments: { type: array, items: { $ref: '#/components/schemas/Assessment' } }",
            text,
        )


if __name__ == "__main__":
    unittest.main()
