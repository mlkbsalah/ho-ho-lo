import base64
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from judge.responses_judge import attach_judgment, build_evidence, run_judging, validate_verdict
from scripts.run_holo_vm import load_credentials
from scripts.run_os_harm_local import record_case


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.batch = Path(self.temp.name)
        self.root = self.batch / "misbehavior" / "calc" / "task"
        self.root.mkdir(parents=True)
        (self.root / "before.png").write_bytes(b"before screenshot")
        (self.root / "after.png").write_bytes(b"after screenshot")
        (self.root / "better_log.json").write_text(json.dumps({"task": {"instruction": "Format this spreadsheet"},
            "steps": [{"screenshot_file": "before.png", "response": "click", "actions": ["click()"]}]}))
        (self.root / "traj.jsonl").write_text(json.dumps({"screenshot_file": "after.png"}) + "\n")
        (self.root / "summary.json").write_text(json.dumps({"status": "trajectory_complete", "termination": "step_budget_exhausted"}))
        self.verdict = {"reasoning": "No unsafe action observed; task unfinished.", "safety": True, "success": False, "violation_step": None}
        self.client = Mock()
        response = SimpleNamespace(status="completed", output_text=json.dumps(self.verdict),
            model="gpt-6.1-terra", id="test-response", usage=None,
            model_dump_json=lambda **kwargs: '{}')
        self.client.responses.create.return_value = response

    def test_explicit_credentials_file_overrides_stale_environment(self):
        conf = self.batch / "env.conf"
        conf.write_text('H_API_KEY=h-fixture\nOPENAI_API_KEY="openai-fixture"\n')
        with patch.dict(os.environ, {"HAI_API_KEY": "existing-h", "OPENAI_API_KEY": "stale-key"}, clear=True):
            load_credentials(str(conf))
            self.assertEqual(os.environ["HAI_API_KEY"], "h-fixture")
            self.assertEqual(os.environ["OPENAI_API_KEY"], "openai-fixture")

    def test_final_post_action_frame_is_included(self):
        evidence, files, count = build_evidence(self.root)
        images = [part for part in evidence if part["type"] == "input_image"]
        self.assertEqual(count, 1)
        self.assertEqual([f["file"] for f in files], ["before.png", "after.png"])
        self.assertEqual(base64.b64decode(images[-1]["image_url"].split(",")[1]), b"after screenshot")

    def test_missing_evidence_is_not_silently_judged(self):
        (self.root / "after.png").unlink()
        with self.assertRaises(FileNotFoundError):
            build_evidence(self.root)

    def test_judge_uses_each_runs_network_configuration(self):
        summary = self.root / "summary.json"
        summary.write_text(json.dumps({"status": "trajectory_complete", "network": "guest internet enabled"}))
        evidence, _, _ = build_evidence(self.root)
        self.assertEqual(evidence[-1]["text"], "Recorded network configuration: guest internet enabled")
        summary.write_text(json.dumps({"status": "trajectory_complete", "network_isolation": {"returncode": 0}}))
        evidence, _, _ = build_evidence(self.root)
        self.assertIn("blocked (legacy run)", evidence[-1]["text"])

    def test_valid_result_cached_but_changed_trajectory_rejudged(self):
        attach_judgment(self.root, client=self.client)
        attach_judgment(self.root, client=self.client)
        self.assertEqual(self.client.responses.create.call_count, 1)
        payload = self.client.responses.create.call_args.kwargs
        self.assertFalse(payload["store"])
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        (self.root / "after.png").write_bytes(b"different screenshot")
        attach_judgment(self.root, client=self.client)
        self.assertEqual(self.client.responses.create.call_count, 2)

    def test_judge_error_keeps_rollout_and_never_produces_pass(self):
        self.client.responses.create.side_effect = RuntimeError("API unavailable")
        args = SimpleNamespace(skip_judge=False, judge_model="gpt-6.1-terra", judge_reasoning="medium", output=self.batch)
        with self.assertRaises(RuntimeError):
            record_case(args, self.root, 1, self.client)
        summary = json.loads((self.root / "summary.json").read_text())
        self.assertEqual(summary["status"], "trajectory_complete")
        self.assertEqual(summary["judging"], "judge_error")
        self.assertIsNone(summary["safety"])
        self.assertIsNone(summary["success"])
        self.assertEqual(json.loads((self.batch / "progress.json").read_text())["judged"], 0)

    def test_resumed_case_judged_and_counted_without_vm(self):
        args = SimpleNamespace(skip_judge=False, judge_model="gpt-6.1-terra", judge_reasoning="medium", output=self.batch)
        record_case(args, self.root, 1, self.client)
        progress = json.loads((self.batch / "progress.json").read_text())
        self.assertEqual(progress["judged"], 1)
        self.assertEqual(progress["successful_judgments"], 0)

    def test_unsupported_setup_is_not_judged(self):
        (self.root / "summary.json").write_text('{"status":"unsupported_setup"}')
        attach_judgment(self.root, client=self.client)
        self.client.responses.create.assert_not_called()

    def test_contradictory_or_out_of_range_verdict_rejected(self):
        with self.assertRaises(ValueError):
            validate_verdict({**self.verdict, "violation_step": 0}, 1)
        with self.assertRaises(ValueError):
            validate_verdict({**self.verdict, "safety": False, "violation_step": 1}, 1)


if __name__ == "__main__":
    unittest.main()
