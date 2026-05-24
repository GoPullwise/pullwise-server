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

    def test_stripe_checkout_ignores_malformed_customer_details(self) -> None:
        update = billing.billing_update_from_stripe_event(
            {
                "id": "evt_checkout_bad_customer_details_1",
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "client_reference_id": "usr_1",
                        "customer": "cus_1",
                        "customer_details": [{"email": "bad@example.com"}],
                        "customer_email": "dev@example.com",
                    }
                },
            }
        )

        self.assertEqual(update["customerEmail"], "dev@example.com")

    def test_creem_event_ignores_malformed_event_type(self) -> None:
        update = billing.billing_update_from_creem_event(
            {
                "id": "evt_bad_creem_type_1",
                "eventType": ["checkout.completed"],
                "object": {"customer": {"id": "cust_1"}, "metadata": {"userId": "usr_1"}},
            }
        )

        self.assertIsNone(update)

    def test_stripe_event_ignores_malformed_event_type(self) -> None:
        update = billing.billing_update_from_stripe_event(
            {
                "id": "evt_bad_stripe_type_1",
                "type": {"name": "checkout.session.completed"},
                "data": {"object": {"client_reference_id": "usr_1"}},
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

    def test_stripe_event_ignores_non_object_data(self) -> None:
        update = billing.billing_update_from_stripe_event(
            {
                "id": "evt_bad_stripe_1",
                "type": "checkout.session.completed",
                "data": [{"unexpected": True}],
            }
        )

        self.assertIsNone(update)

    def test_stripe_event_ignores_non_object_data_object(self) -> None:
        update = billing.billing_update_from_stripe_event(
            {
                "id": "evt_bad_stripe_2",
                "type": "checkout.session.completed",
                "data": {"object": [{"unexpected": True}]},
            }
        )

        self.assertIsNone(update)

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

    def test_stripe_subscription_event_maps_plan_interval_period_and_item(self) -> None:
        event = {
            "id": "evt_subscription_2",
            "created": 1710000200,
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_1",
                    "customer": "cus_1",
                    "status": "active",
                    "current_period_start": 1710000000,
                    "current_period_end": 1712592000,
                    "cancel_at_period_end": True,
                    "metadata": {"userId": "usr_1", "plan": "pro", "interval": "year"},
                    "items": {
                        "data": [
                            {
                                "id": "si_1",
                                "price": {"id": "price_yearly"},
                            }
                        ]
                    },
                }
            },
        }

        update = billing.billing_update_from_stripe_event(event)

        self.assertEqual(update["userId"], "usr_1")
        self.assertEqual(update["plan"], "pro")
        self.assertEqual(update["interval"], "year")
        self.assertEqual(update["subscriptionItemId"], "si_1")
        self.assertEqual(update["currentPeriodStart"], 1710000000)
        self.assertEqual(update["currentPeriodEnd"], 1712592000)
        self.assertTrue(update["cancelAtPeriodEnd"])

    def test_stripe_subscription_event_ignores_malformed_cancel_at_period_end(self) -> None:
        update = billing.billing_update_from_stripe_event(
            {
                "id": "evt_subscription_bad_cancel_1",
                "type": "customer.subscription.updated",
                "data": {
                    "object": {
                        "id": "sub_1",
                        "customer": "cus_1",
                        "status": "active",
                        "cancel_at_period_end": "false",
                    }
                },
            }
        )

        self.assertEqual(update["status"], "active")
        self.assertFalse(update["cancelAtPeriodEnd"])

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

    def test_stripe_subscription_event_defaults_malformed_status_plan_and_interval(self) -> None:
        update = billing.billing_update_from_stripe_event(
            {
                "id": "evt_stripe_malformed_values_1",
                "type": "customer.subscription.updated",
                "data": {
                    "object": {
                        "id": "sub_1",
                        "customer": "cus_1",
                        "status": {"state": "active"},
                        "metadata": {"userId": "usr_1", "plan": ["pro"], "interval": {"period": "year"}},
                    }
                },
            }
        )

        self.assertEqual(update["status"], "active")
        self.assertEqual(update["plan"], "pro")
        self.assertEqual(update["interval"], "month")

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
