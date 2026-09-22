import os
from pathlib import Path
import tempfile
import unittest

from labmon.estimates import Estimator, MAX_TAIL, parse_progress, safe_tail


class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "job.sta"
        self.now = 1_800_000_000.0
        self.estimator = Estimator([str(self.root)], clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def step(self, progress, elapsed=5, *, append=True):
        self.now += elapsed
        line = f"1 1 1 0 3 3 {progress:.6f} {progress:.6f} .1\n"
        with self.log.open("a" if append else "w", encoding="utf-8") as stream:
            stream.write(line)
        return self.estimator.estimate(str(self.log), "abaqus", 100)

    def test_requires_known_target_and_wall_clock_progress(self):
        self.step(10)
        result = self.estimator.estimate(str(self.log), "abaqus", None)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["remaining_seconds"])
        self.assertEqual(self.step(20)["status"], "warming")
        result = self.step(30)
        self.assertEqual(result["status"], "estimating")
        self.assertAlmostEqual(result["remaining_seconds"], 35)

    def test_no_progress_suspends_estimate(self):
        self.step(10)
        self.step(20)
        self.step(30)
        self.now += 35
        result = self.estimator.estimate(str(self.log), "abaqus", 100)
        self.assertEqual(result["status"], "no-progress")
        self.assertIsNone(result["estimated_end"])

    def test_progress_reset_and_truncation_restart_sampling(self):
        self.step(10)
        self.step(20)
        self.step(30)
        result = self.step(5, append=False)
        self.assertEqual(result["status"], "warming")
        self.assertIsNone(result["remaining_seconds"])
        self.assertIn("截断", result["detail"])

    def test_replaced_file_restarts_sampling(self):
        self.step(10)
        self.step(20)
        self.step(30)
        replacement = self.root / "replacement.sta"
        replacement.write_text("1 1 1 0 3 3 40 40 .1\n", encoding="utf-8")
        os.replace(replacement, self.log)
        self.now += 5
        self.assertEqual(self.estimator.estimate(str(self.log), "abaqus", 100)["status"], "warming")

    def test_target_is_not_success_claim(self):
        result = self.step(100)
        self.assertEqual(result["status"], "completed")
        self.assertIn("不代表", result["detail"])
        self.log.write_text("THE ANALYSIS HAS COMPLETED SUCCESSFULLY\n", encoding="utf-8")
        result = self.estimator.estimate(str(self.log), "abaqus")
        self.assertEqual(result["status"], "completed")
        self.assertIn("明确", result["detail"])

    def test_appended_new_run_does_not_reuse_old_completion_marker(self):
        self.log.write_text("THE ANALYSIS HAS COMPLETED SUCCESSFULLY\n1 1 1 0 1 1 .1 .1 .1\n", encoding="utf-8")
        result = self.estimator.estimate(str(self.log), "abaqus", 1)
        self.assertEqual(result["status"], "warming")
        self.assertEqual(result["progress_pct"], 10)

    def test_changing_rate_uses_recent_observations(self):
        self.step(1)
        self.step(2)
        early = self.step(3)
        for progress in range(13, 84, 10):
            latest = self.step(progress)
        self.assertLess(latest["remaining_seconds"], early["remaining_seconds"])
        self.assertGreaterEqual(latest["remaining_seconds"], 0)

    def test_fluent_header_required_and_comsol_time_explicit(self):
        self.assertIsNone(parse_progress("10 1e-4 2e-5\n", "fluent")[0])
        self.assertEqual(parse_progress("iter continuity x-velocity\n10 1e-4 2e-5\n", "fluent")[0], 10)
        self.assertEqual(parse_progress("Time = 0.2\nTime = 0.3\n", "comsol")[0], .3)
        self.assertIsNone(parse_progress("Meshing Progress: 99%\n", "comsol")[0])

    def test_unknown_log_never_uses_cpu_or_manual_duration(self):
        self.log.write_text("CPU=100% elapsed=999 remaining=20\n", encoding="utf-8")
        result = self.estimator.estimate(str(self.log), "abaqus", 100)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["remaining_seconds"])

    def test_path_boundaries_extensions_traversal(self):
        self.log.write_text("test", encoding="utf-8")
        for bad in ("job.sta", str(self.root / ".." / self.root.name / "job.sta"), "\\\\server\\share\\job.sta"):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                safe_tail(bad, [str(self.root)])
        bad_ext = self.root / "private.key"
        bad_ext.write_text("secret", encoding="utf-8")
        with self.assertRaises(ValueError):
            safe_tail(str(bad_ext), [str(self.root)])
        with tempfile.TemporaryDirectory() as outside:
            outsider = Path(outside) / "outside.log"
            outsider.write_text("private", encoding="utf-8")
            with self.assertRaises(ValueError):
                safe_tail(str(outsider), [str(self.root)])

    def test_symlink_rejected_even_if_destination_allowed(self):
        self.log.write_text("test", encoding="utf-8")
        link = self.root / "linked.sta"
        try:
            link.symlink_to(self.log)
        except OSError:
            self.skipTest("Windows account lacks symbolic-link privilege")
        with self.assertRaises(ValueError):
            safe_tail(str(link), [str(self.root)])

    def test_reads_at_most_tail_and_does_not_return_raw_log(self):
        self.log.write_text("sensitive first line\n" + ("x" * 100 + "\n") * 5000 + "1 1 1 0 3 3 .5 .5 .1\n", encoding="utf-8")
        text, info = safe_tail(str(self.log), [str(self.root)])
        self.assertLessEqual(len(text.encode()), MAX_TAIL)
        self.assertNotIn("sensitive first line", text)
        result = self.estimator.estimate(str(self.log), "abaqus", 1)
        self.assertNotIn("text", result)
        self.assertNotIn("path", result)
        self.assertEqual(result["progress_pct"], 50)

    def test_invalid_totals(self):
        self.log.write_text("1 1 1 0 3 3 1 1 .1\n", encoding="utf-8")
        for value in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.estimator.estimate(str(self.log), "abaqus", value)


if __name__ == "__main__":
    unittest.main()
