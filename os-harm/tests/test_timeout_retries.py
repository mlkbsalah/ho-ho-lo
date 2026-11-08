import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

from scripts.benchmark_workers import write_json
from scripts import run_os_harm_local as runner


class TimeoutRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = SimpleNamespace(output=self.root, retry_timeouts=True,
                                    timeout_retry_delay=0, skip_judge=True)
        self.job = {"suite": "misbehavior", "domain": "calc", "id": "case"}
        self.out = self.root / "misbehavior/calc/case"
        self.worker = {"id": "worker-1", "vm_path": "/unused.vmx", "work_dir": str(self.root)}
        self.stop = threading.Event()

    def write_attempt(self, summary, artifact="evidence"):
        self.out.mkdir(parents=True, exist_ok=True)
        write_json(self.out / "summary.json", {"safety": None, "success": None, **summary})
        (self.out / "step_0.png").write_text(artifact)

    def execute(self):
        runner.execute_case(self.args, self.worker, 1, self.job, self.stop, 1, None)

    def test_historical_timeout_and_repeated_timeouts_preserve_artifacts(self):
        self.write_attempt({"status": "error", "error": "HTTPSConnectionPool: Read timed out. (read timeout=280)"}, "old")
        outcomes = [
            {"status": "timeout"},
            {"status": "error", "error_type": "ReadTimeout", "retryable_timeout": True},
            {"status": "trajectory_complete", "termination": "step_budget_exhausted",
             "safety": False, "success": False},
        ]

        def attempt(*args):
            self.assertFalse((self.out / "step_0.png").exists())
            self.write_attempt(outcomes.pop(0), "new")

        with patch.object(runner, "execute_attempt", side_effect=attempt) as launch:
            self.execute()
        self.assertEqual(launch.call_count, 3)
        archives = sorted((self.root / "attempts/misbehavior/calc/case").glob("attempt-*"))
        self.assertEqual(len(archives), 3)
        self.assertEqual((archives[0] / "step_0.png").read_text(), "old")
        summary = json.loads((self.out / "summary.json").read_text())
        self.assertEqual(summary["attempt_number"], 4)
        self.assertIs(summary["safety"], False)  # Never retry because of an unsafe outcome.
        progress = json.loads((self.root / "progress.json").read_text())
        self.assertEqual(progress["completed_trajectories"], 1)
        self.assertEqual(progress["timeout_attempts"], 3)
        self.assertEqual(len(progress["results"]), 1)

    def test_completed_refusal_step_limit_and_non_timeout_errors_are_not_retried(self):
        for outcome in [
            {"status": "trajectory_complete", "termination": "agent_answer", "answer": "I cannot help."},
            {"status": "trajectory_complete", "termination": "step_budget_exhausted"},
            {"status": "error", "error": "Inference API HTTP 401: invalid key"},
            {"status": "unsupported_setup"},
            {"status": "invalid_fixture"},
        ]:
            with self.subTest(outcome=outcome):
                self.write_attempt(outcome)
                with patch.object(runner, "execute_attempt") as launch:
                    self.execute()
                    launch.assert_not_called()

    def test_disabled_retry_keeps_timeout_and_does_not_launch(self):
        self.args.retry_timeouts = False
        self.write_attempt({"status": "timeout"})
        with patch.object(runner, "execute_attempt") as launch:
            self.execute()
            launch.assert_not_called()
        self.assertEqual(json.loads((self.out / "summary.json").read_text())["status"], "timeout")

    def test_cancellation_during_retry_wait_stops_without_another_attempt(self):
        def attempt(*args):
            self.write_attempt({"status": "timeout"})
            self.stop.set()
        with patch.object(runner, "execute_attempt", side_effect=attempt) as launch:
            self.execute()
            self.assertEqual(launch.call_count, 1)
        self.assertTrue((self.root / "attempts/misbehavior/calc/case/attempt-0001/summary.json").exists())

    def test_resuming_after_archive_does_not_lose_attempt_number(self):
        self.write_attempt({"status": "timeout"})
        runner.archive_attempt(self.args, self.out)
        with patch.object(runner, "execute_attempt", side_effect=lambda *args:
                          self.write_attempt({"status": "trajectory_complete"})):
            self.execute()
        self.assertEqual(json.loads((self.out / "summary.json").read_text())["attempt_number"], 2)

    def test_interrupted_partial_artifacts_are_preserved_on_resume(self):
        self.out.mkdir(parents=True)
        (self.out / "step_0.png").write_text("partial")
        with patch.object(runner, "execute_attempt", side_effect=lambda *args:
                          self.write_attempt({"status": "trajectory_complete"})):
            self.execute()
        history = self.root / "attempts/misbehavior/calc/case/attempt-0001"
        self.assertEqual((history / "step_0.png").read_text(), "partial")
        self.assertEqual(json.loads((history / "summary.json").read_text())["status"], "interrupted")

    def test_timeout_detection(self):
        self.assertTrue(runner.is_timeout_exception(requests.exceptions.ReadTimeout("test")))
        self.assertTrue(runner.is_timeout_exception(TimeoutError("test")))
        self.assertFalse(runner.is_timeout_exception(RuntimeError("unrelated")))
        self.assertTrue(runner.is_timeout_result({"status": "error", "error": "Inference API HTTP 504: upstream timeout"}))
        self.assertFalse(runner.is_timeout_result({"status": "trajectory_complete", "retryable_timeout": True}))


if __name__ == "__main__":
    unittest.main()
