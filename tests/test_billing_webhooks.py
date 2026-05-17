from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
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

    def test_verifies_stripe_signature_header_over_timestamped_raw_body(self) -> None:
        raw = b'{"type":"checkout.session.completed"}'
        timestamp = int(time.time())
        signed = f"{timestamp}.{raw.decode('utf-8')}".encode("utf-8")
        signature = hmac.new(b"whsec_test", signed, hashlib.sha256).hexdigest()

        with patch.dict(os.environ, {"PULLWISE_STRIPE_WEBHOOK_SECRET": "whsec_test"}, clear=True):
            self.assertTrue(billing.verify_stripe_webhook(raw, f"t={timestamp},v1={signature}"))
            self.assertFalse(billing.verify_stripe_webhook(raw, f"t={timestamp},v1={'0' * 64}"))

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

    def test_stripe_checkout_completed_maps_to_active_billing(self) -> None:
        event = {
            "id": "evt_checkout_1",
            "created": 1710000000,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": "usr_1",
                    "customer": "cus_1",
                    "subscription": "sub_1",
                }
            },
        }

        update = billing.billing_update_from_stripe_event(event)

        self.assertEqual(update["userId"], "usr_1")
        self.assertEqual(update["provider"], "stripe")
        self.assertEqual(update["customerId"], "cus_1")
        self.assertEqual(update["subscriptionId"], "sub_1")
        self.assertEqual(update["status"], "active")
        self.assertEqual(update["eventId"], "evt_checkout_1")
        self.assertEqual(update["eventCreated"], 1710000000)

    def test_stripe_subscription_event_includes_event_metadata_for_idempotency(self) -> None:
        event = {
            "id": "evt_subscription_1",
            "created": 1710000100,
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_1",
                    "customer": "cus_1",
                    "status": "past_due",
                }
            },
        }

        update = billing.billing_update_from_stripe_event(event)

        self.assertEqual(update["customerId"], "cus_1")
        self.assertEqual(update["subscriptionId"], "sub_1")
        self.assertEqual(update["status"], "past_due")
        self.assertEqual(update["eventId"], "evt_subscription_1")
        self.assertEqual(update["eventCreated"], 1710000100)


if __name__ == "__main__":
    unittest.main()
