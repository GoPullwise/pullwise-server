"""Emit fresh SQLite -> shared REST DTOs for the sibling Web contract test.

Synthetic analysis only. No GitHub, model, deployment, or persistent user data.
"""
import json
import os
from unittest.mock import patch

from pullwise_server import product_api
from test_source_assessment_persistence import publication
from test_saved_updates_projection import assessment


class Handler:
    headers = {"X-Request-Id": "contract"}

    def current_session(self):
        return {"userId": "owner"}

    def current_api_key_context(self):
        return None

    def json(self, payload, status=200, **kwargs):
        assert status == 200, payload
        self.payload = payload


class KeyHandler(Handler):
    def current_session(self):
        return None

    def current_api_key_context(self):
        return {"user": {"id": "owner"}, "apiKey": {"id": "synthetic-key"}, "scopes": ["items:read"],
                "restrictions": {"watchIds": ["watch1"]}}


def export_contract():
    fixture = publication.__wrapped__()
    store, args, context = next(fixture)
    try:
        coverage = {"state": "complete", "selectedUnits": 1, "totalUnits": 1, "omittedUnits": 0,
                    "rawSourcePartial": False, "selectionRuleVersion": "updates-units/v1", "limitations": []}
        store.set_source_context(**context, coverage=coverage)
        saved = assessment([("not_relevant", "absent", "absent", "absent", "absent")])
        for binding in saved["bindings"].values():
            binding["evidenceIds"] = ["e1"]
        args["assessment"].update(saved)
        store.publish_assessment_result(**args)
        before = store.processing_usage(billing_owner_id="owner", period="period")
        with patch.dict(os.environ, {"PULLWISE_DB_PATH": store.database_path}):
            responses = {}
            for name, segments in (("listing", ["sources"]), ("detail", ["sources", "release"])):
                handler = Handler()
                assert product_api.handle_get(handler, segments, {"module": "updates"}, {"owner": {"id": "owner"}})
                responses[name] = handler.payload
                key = KeyHandler()
                assert product_api.handle_get(key, segments, {"module": "updates"}, {})
                assert key.payload == handler.payload
        assert before == store.processing_usage(billing_owner_id="owner", period="period")
        assert store.count_jobs(job_type="analyze_source") == 0
        return responses
    finally:
        fixture.close()


if __name__ == "__main__":
    print(json.dumps(export_contract()))
