from __future__ import annotations

import hashlib
import hmac
import os
import unittest
from unittest.mock import patch

from pullwise_server import billing


class BillingWebhookTest(unittest.TestCase):
    def test_verifies_creem_hmac_signature_over_raw_body(self) -> None:
        raw = b'{"eventType":"checkout.completed"}'
        signature = hmac.new(b"whsec_test", raw, hashlib.sha256).hexdigest()

        with patch.dict(os.environ, {"PULLWISE_CREEM_WEBHOOK_SECRET": "whsec_test"}, clear=True):
            self.assertTrue(billing.verify_creem_webhook(raw, signature))
            self.assertFalse(billing.verify_creem_webhook(raw, "0" * 64))

    def test_creem_checkout_completed_maps_to_active_billing(self) -> None:
        event = {
            "id": "evt_creem_checkout_1",
            "created": 1710000200,
            "eventType": "checkout.completed",
            "object": {
                "customer": {"id": "cust_1", "email": "dev@example.com"},
                "subscription": {"id": "sub_1", "status": "active"},
                "metadata": {"userId": "usr_1"},
            },
        }

        update = billing.billing_update_from_creem_event(event)

        self.assertEqual(update["userId"], "usr_1")
        self.assertEqual(update["provider"], "creem")
        self.assertEqual(update["customerId"], "cust_1")
        self.assertEqual(update["subscriptionId"], "sub_1")
        self.assertEqual(update["status"], "active")
        self.assertEqual(update["eventId"], "evt_creem_checkout_1")
        self.assertEqual(update["eventCreated"], 1710000200)

    def test_creem_event_ignores_malformed_event_type(self) -> None:
        update = billing.billing_update_from_creem_event(
            {
                "id": "evt_bad_creem_type_1",
                "eventType": ["checkout.completed"],
                "object": {"customer": {"id": "cust_1"}, "metadata": {"userId": "usr_1"}},
            }
        )

        self.assertIsNone(update)

    def test_creem_event_ignores_non_object_payload(self) -> None:
        update = billing.billing_update_from_creem_event(
            {
                "id": "evt_bad_creem_1",
                "eventType": "checkout.completed",
                "object": [{"unexpected": True}],
            }
        )

        self.assertIsNone(update)

    def test_creem_event_defaults_malformed_status_plan_and_interval(self) -> None:
        update = billing.billing_update_from_creem_event(
            {
                "id": "evt_creem_malformed_values_1",
                "eventType": "checkout.completed",
                "object": {
                    "customer": {"id": "cust_1"},
                    "subscription": {"id": "sub_1", "status": {"state": "active"}},
                    "metadata": {"userId": "usr_1", "plan": {"tier": "pro"}, "interval": ["year"]},
                },
            }
        )

        self.assertEqual(update["status"], "active")
        self.assertEqual(update["plan"], "pro")
        self.assertEqual(update["interval"], "month")

    def test_creem_product_id_maps_to_configured_interval(self) -> None:
        event = {
            "id": "evt_creem_product_interval_1",
            "eventType": "subscription.update",
            "object": {
                "id": "sub_1",
                "status": "active",
                "product": {"id": "prod_yearly", "billing_period": "every-month"},
                "customer": {"id": "cust_1"},
                "metadata": {"userId": "usr_1"},
            },
        }

        with patch.dict(os.environ, {"PULLWISE_CREEM_PRO_YEARLY_PRODUCT_ID": "prod_yearly"}, clear=True):
            update = billing.billing_update_from_creem_event(event)

        self.assertEqual(update["interval"], "year")

    def test_creem_expired_subscription_is_not_mapped_to_canceled(self) -> None:
        event = {
            "id": "evt_creem_expired_1",
            "created": 1710000300,
            "eventType": "subscription.expired",
            "object": {
                "id": "sub_1",
                "status": "active",
                "current_period_start_date": "2024-03-01T00:00:00.000Z",
                "current_period_end_date": "2024-04-01T00:00:00.000Z",
                "product": {
                    "id": "prod_yearly",
                    "billing_period": "every-year",
                },
                "customer": {"id": "cust_1", "email": "dev@example.com"},
                "metadata": {"userId": "usr_1", "plan": "pro"},
            },
        }

        update = billing.billing_update_from_creem_event(event)

        self.assertEqual(update["status"], "past_due")
        self.assertEqual(update["interval"], "year")
        self.assertEqual(update["currentPeriodStart"], "2024-03-01T00:00:00.000Z")
        self.assertEqual(update["currentPeriodEnd"], "2024-04-01T00:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
