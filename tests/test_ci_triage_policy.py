import unittest

from pullwise_server.ci_triage_policy import AUTO_LABEL_POLICY, publish_symptom


class CiTriagePolicyTest(unittest.TestCase):
    def test_versioned_default_keeps_existing_boundary(self):
        self.assertEqual(AUTO_LABEL_POLICY.version, "ci-auto-label/v1")
        self.assertFalse(publish_symptom({"choice": "present", "confidence": 0.799}))
        self.assertTrue(publish_symptom({"choice": "present", "confidence": 0.8}))
        self.assertFalse(publish_symptom({"choice": "absent", "confidence": 1.0}))
        self.assertFalse(publish_symptom({"choice": "unclear", "confidence": 1.0}))

    def test_malformed_confidence_fails_closed(self):
        for confidence in (None, True, "0.9", float("nan"), -1, 1.1):
            self.assertFalse(publish_symptom({"choice": "present", "confidence": confidence}))
