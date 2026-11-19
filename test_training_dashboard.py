"""Tests for the standalone Ubuntu training monitor."""
import json
from pathlib import Path
import tempfile
import unittest

from training_dashboard import collect_runs, run_detail


class TrainingDashboardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runs = Path(temporary.name)

    def write(self, name, data):
        directory = self.runs / name
        directory.mkdir()
        (directory / "progress.json").write_text(json.dumps(data), encoding="utf-8")
        return directory

    def test_collect_runs_orders_running_first_and_keeps_waiting_runs(self):
        self.write("completed", {"status": "completed", "step": 20, "total_steps": 20})
        self.write("running", {"status": "running", "step": 4, "total_steps": 20, "loss": 1.5,
                               "validation_trend": [{"step": 1, "mean": 1.8}]})
        waiting = self.runs / "waiting"
        waiting.mkdir()
        (waiting / "task.zip").write_bytes(b"zip")

        runs = collect_runs(self.runs)
        self.assertEqual(runs[0]["id"], "running")
        self.assertEqual({run["id"] for run in runs[1:]}, {"completed", "waiting"})
        self.assertEqual(runs[0]["validation_mean"], 1.8)
        waiting_run = next(run for run in runs if run["id"] == "waiting")
        self.assertEqual(waiting_run["status"], "waiting")
        self.assertTrue(waiting_run["task"])

    def test_detail_includes_log_tail_and_result_link(self):
        directory = self.write("demo", {"status": "completed", "step": 2, "total_steps": 2})
        (directory / "training.log").write_text("first\nlast\n", encoding="utf-8")
        (directory / "result.zip").write_bytes(b"zip")

        detail = run_detail(self.runs, "demo")
        self.assertEqual(detail["log"], "first\nlast\n")
        self.assertEqual(detail["links"]["result"], "/api/training/runs/demo/result.zip")
        self.assertEqual(detail["progress"]["step"], 2)

    def test_detail_rejects_unknown_and_traversal_ids(self):
        self.assertIsNone(run_detail(self.runs, "../outside"))
        self.assertIsNone(run_detail(self.runs, "missing"))

    def test_detail_accepts_chinese_run_directory(self):
        directory = self.write("声音-a", {"status": "running", "step": 1, "total_steps": 2})
        self.assertEqual(run_detail(self.runs, directory.name)["id"], "声音-a")
        (directory / "result.zip").write_bytes(b"zip")
        self.assertEqual(run_detail(self.runs, directory.name)["links"]["result"],
                         "/api/training/runs/%E5%A3%B0%E9%9F%B3-a/result.zip")

    def test_collect_runs_ignores_empty_directories(self):
        (self.runs / "empty").mkdir()
        self.assertEqual(collect_runs(self.runs), [])


if __name__ == "__main__":
    unittest.main()
