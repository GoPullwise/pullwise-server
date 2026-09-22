from __future__ import annotations

import unittest

from pullwise_server.entitlements import entitlements_for_user


class ProductEntitlementsTest(unittest.TestCase):
    def test_plan_capacities_use_one_current_source(self) -> None:
        self.assertEqual(
            entitlements_for_user({"id": "usr_1", "billing": {"plan": "free", "status": "active"}}, timestamp=1_800_000_000)["entitlements"],
            {
                "activeRepositoryLimit": 1,
                "activeWatchLimit": 3,
                "monthlyProcessingLimit": 200,
            },
        )
        self.assertEqual(
            entitlements_for_user({"id": "usr_1", "billing": {"plan": "pro", "status": "active"}}, timestamp=1_800_000_000)["entitlements"]["monthlyProcessingLimit"],
            5000,
        )
        self.assertEqual(
            entitlements_for_user({"id": "usr_1", "billing": {"plan": "max", "status": "active"}}, timestamp=1_800_000_000)["entitlements"]["activeWatchLimit"],
            100,
        )

    def test_expired_paid_subscription_maps_to_free_without_changing_billing_fact(self) -> None:
        user = {
            "id": "usr_1",
            "billing": {
                "plan": "pro",
                "status": "canceling",
                "currentPeriodStart": 1_700_000_000,
                "currentPeriodEnd": 1_750_000_000,
                "subscriptionId": "sub_preserved",
            },
        }

        result = entitlements_for_user(user, timestamp=1_800_000_000)

        self.assertEqual(result["plan"], "free")
        self.assertEqual(user["billing"]["subscriptionId"], "sub_preserved")


if __name__ == "__main__":
    unittest.main()
