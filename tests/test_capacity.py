"""Read-only preflight advice must fail closed on stale or incomplete data."""
import unittest
from datetime import datetime, timezone

from labmon.capacity import GIB, capacity_report
from labmon.server import parse_capacity_query

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc).timestamp() + 10


def server(status="online", age=2):
    return {"id": "compute", "name": "计算机", "status": status,
            "last_seen": f"2026-09-24T00:00:{10-age:02d}+00:00", "jobs": [{}],
            "snapshot": {"telemetry_status": "ok", "cpu": {"logical_processors": 64, "percent": 25},
                         "memory": {"total_bytes": 128 * GIB, "available_bytes": 80 * GIB},
                         "gpus": [{"id": "0", "name": "GPU", "memory_total_bytes": 24 * GIB,
                                   "memory_used_bytes": 4 * GIB, "utilization_pct": 10}],
                         "ssh": {"tcp_connections": 2}}}


class CapacityTests(unittest.TestCase):
    def test_estimate_uses_reserves_and_reports_claims(self):
        request = {"cpu_cores": 20, "memory_bytes": 32 * GIB, "gpu_memory_bytes": 8 * GIB}
        report = capacity_report(server(), request, NOW, 30, active_claims=1)
        self.assertEqual(report["verdict"], "likely_available")
        self.assertEqual(report["cpu"]["estimated_usable_logical_cores"], 41)
        self.assertEqual(report["memory"]["estimated_usable_bytes"], int(73.6 * GIB))
        self.assertEqual(report["active_claims"], 1)
        self.assertEqual(report["ssh_connections"], 2)

    def test_insufficient_and_unknown_are_distinct(self):
        cpu = {"cpu_cores": 48, "memory_bytes": 0, "gpu_memory_bytes": 0}
        self.assertEqual(capacity_report(server(), cpu, NOW, 30)["verdict"], "insufficient")
        self.assertEqual(capacity_report(server(status="offline"), cpu, NOW, 30)["verdict"], "unknown")
        stale = server(); stale["last_seen"] = "2026-09-23T23:58:00+00:00"
        self.assertEqual(capacity_report(stale, cpu, NOW, 30)["verdict"], "unknown")
        missing = server(); missing["snapshot"]["cpu"]["percent"] = None
        self.assertEqual(capacity_report(missing, cpu, NOW, 30)["verdict"], "unknown")

    def test_partial_cpu_coverage_is_not_reported_as_free_cores(self):
        partial = server()
        partial["snapshot"]["cpu"]["observed_processors"] = 32
        report = capacity_report(partial, {"cpu_cores": 8, "memory_bytes": 0,
                                           "gpu_memory_bytes": 0}, NOW, 30)
        self.assertIsNone(report["cpu"]["estimated_usable_logical_cores"])
        self.assertEqual(report["verdict"], "unknown")

    def test_invalid_query_is_rejected(self):
        self.assertEqual(parse_capacity_query("host_id=compute&cpu_cores=16&memory_gb=32"),
                         ("compute", {"cpu_cores": 16, "memory_bytes": 32 * GIB, "gpu_memory_bytes": 0}))
        for query in ("cpu_cores=-1", "cpu_cores=nan", "memory_gb=inf", "cpu_cores=1&cpu_cores=2", "evil=1"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                parse_capacity_query(query)


if __name__ == "__main__":
    unittest.main()
