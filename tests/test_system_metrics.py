from __future__ import annotations

import tempfile
import unittest

from pullwise_server import system_metrics


class SystemMetricsTest(unittest.TestCase):
    def test_server_metrics_payload_includes_machine_resource_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = system_metrics.server_metrics_payload(storage_path=temp_dir, timestamp=1781200000)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["collectedAt"], 1781200000)
        self.assertNotIn("usagePercent", payload["cpu"])
        self.assertIn("logicalCount", payload["cpu"])
        self.assertIn("availableBytes", payload["memory"])
        self.assertIn("totalBytes", payload["memory"])
        self.assertIn("freeBytes", payload["storage"])
        self.assertIn("totalBytes", payload["storage"])
        self.assertIn("hostname", payload["server"])

    def test_server_metrics_history_records_compact_resource_points(self) -> None:
        payload = {
            "collectedAt": 1781200000,
            "cpu": {
                "logicalCount": 8,
                "loadAverage": {"oneMinute": 1.25, "fiveMinute": 0.75, "fifteenMinute": 0.5},
            },
            "memory": {"totalBytes": 1000, "availableBytes": 250, "usedBytes": 750, "usedPercent": 75.0},
            "storage": {"totalBytes": 2000, "freeBytes": 1000, "usedBytes": 1000, "usedPercent": 50.0},
        }

        history = system_metrics.server_metrics_history([], payload)

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["collectedAt"], 1781200000)
        self.assertEqual(history[0]["memory"]["usedPercent"], 75.0)
        self.assertEqual(history[0]["storage"]["usedPercent"], 50.0)
        self.assertEqual(history[0]["cpu"]["loadAverage"]["oneMinute"], 1.25)
        self.assertNotIn("usagePercent", str(history))

    def test_server_metrics_history_replaces_recent_sample_and_prunes_limit(self) -> None:
        first = {
            "collectedAt": 1781200000,
            "cpu": {"logicalCount": 8, "loadAverage": None},
            "memory": {"usedPercent": 50.0},
            "storage": {"usedPercent": 40.0},
        }
        second = {
            "collectedAt": 1781200005,
            "cpu": {"logicalCount": 8, "loadAverage": None},
            "memory": {"usedPercent": 51.0},
            "storage": {"usedPercent": 41.0},
        }
        third = {
            "collectedAt": 1781200020,
            "cpu": {"logicalCount": 8, "loadAverage": None},
            "memory": {"usedPercent": 52.0},
            "storage": {"usedPercent": 42.0},
        }

        history = system_metrics.server_metrics_history([], first)
        history = system_metrics.server_metrics_history(history, second, min_interval_seconds=10)
        history = system_metrics.server_metrics_history(history, third, limit=1, min_interval_seconds=10)

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["collectedAt"], 1781200020)
        self.assertEqual(history[0]["memory"]["usedPercent"], 52.0)


if __name__ == "__main__":
    unittest.main()
