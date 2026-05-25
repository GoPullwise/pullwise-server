from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from pullwise_server import app, db, quota


class LegacyWorkspaceMigrationTest(unittest.TestCase):
    def test_user_billing_usage_migrates_to_workspace_quota_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": os.path.join(tmpdir, "pullwise.sqlite3")}, clear=False):
                workspace = db.upsert_workspace({"id": "ws_1", "name": "acme"})
                user = {
                    "id": "usr_1",
                    "billingUsage": {
                        "period": quota.current_period(),
                        "plan": "free",
                        "used": 2,
                    },
                }

                updated = app.migrate_user_billing_to_workspace(user, workspace)
                usage = quota.quota_payload_for_workspace(updated)

        self.assertEqual(usage["used"], 2)

    def test_user_subscription_metadata_migrates_to_empty_workspace_billing_subject(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": os.path.join(tmpdir, "pullwise.sqlite3")}, clear=False):
                workspace = db.upsert_workspace({"id": "ws_1", "name": "acme"})
                user = {
                    "id": "usr_1",
                    "billing": {
                        "provider": "stripe",
                        "customerId": "cus_1",
                        "subscriptionId": "sub_1",
                        "subscriptionItemId": "si_1",
                        "status": "active",
                        "plan": "pro",
                        "interval": "year",
                    },
                }

                updated = app.migrate_user_billing_to_workspace(user, workspace)

        self.assertEqual(updated["billing_customer_id"], "cus_1")
        self.assertEqual(updated["billing_subscription_id"], "sub_1")
        self.assertEqual(updated["plan"], "pro")
        self.assertEqual(updated["billing_interval"], "year")


if __name__ == "__main__":
    unittest.main()
