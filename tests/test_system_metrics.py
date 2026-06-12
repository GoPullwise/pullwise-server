from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from pullwise_server import system_metrics


class SystemMetricsTest(unittest.TestCase):
    def test_server_metrics_payload_includes_machine_resource_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = system_metrics.server_metrics_payload(storage_path=temp_dir, timestamp=1781200000)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["collectedAt"], 1781200000)
        self.assertIn("usagePercent", payload["cpu"])
        self.assertIn("logicalCount", payload["cpu"])
        self.assertIn("availableBytes", payload["memory"])
        self.assertIn("totalBytes", payload["memory"])
        self.assertIn("freeBytes", payload["storage"])
        self.assertIn("totalBytes", payload["storage"])
        self.assertIn("hostname", payload["server"])

    def test_cpu_usage_percent_uses_proc_stat_delta(self) -> None:
        with (
            patch.object(system_metrics, "_read_proc_cpu_times", side_effect=[(100, 200), (125, 300)]),
            patch.object(system_metrics.time, "sleep") as sleep,
        ):
            usage = system_metrics.cpu_usage_percent(sample_interval_seconds=0.01)

        self.assertEqual(usage, 75.0)
        sleep.assert_called_once_with(0.01)


if __name__ == "__main__":
    unittest.main()
