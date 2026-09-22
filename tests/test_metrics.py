import unittest
from labmon.metrics import render_metrics
from labmon.collector import parse_metrics


class MetricsTests(unittest.TestCase):
    def test_unknowns_omitted_and_labels_round_trip(self):
        name = 'GPU "示例"\\设备\nline'
        snapshot = {"cpu": {"percent": 0, "logical_processors": 48}, "memory": {"percent": None},
                    "gpus": [{"id": "0", "name": name, "utilization_pct": 12, "power_w": float("nan")}],
                    "processes": [{"pid": 42, "name": "python", "cpu_pct": 2.5}]}
        parsed = parse_metrics(render_metrics(snapshot).decode())
        self.assertEqual(parsed["labmon_cpu_percent"][0][1], 0)
        self.assertNotIn("labmon_memory_percent", parsed)
        self.assertNotIn("labmon_gpu_power_watts", parsed)
        self.assertEqual(parsed["labmon_gpu_utilization_percent"][0][0]["name"], name)
        self.assertEqual(parsed["labmon_process_cpu_percent"][0][1], 2.5)


if __name__ == "__main__":
    unittest.main()
