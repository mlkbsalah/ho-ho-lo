"""Reporting guards against false safety credit and stale/cross-cell evidence."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from build_pilot_report import build_report, EXPECTED_PROTOCOL_SHA256

SOURCE = Path(__file__).resolve().parents[1] / "research-design.json"


class ReportingGuards(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="osharm-report-fixture-")
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.protocol = self.base / "research-design.json"
        self.protocol.write_bytes(SOURCE.read_bytes())
        self.plan = json.loads(self.protocol.read_text())
        self.pilot = self.base / "runs" / self.plan["pilot_id"]

    def report(self):
        return build_report(self.protocol, self.pilot)

    def attempt(self, status="trajectory_complete", exposure="Yes", adoption="No", endorsement="No"):
        ep = self.plan["schedule"][0]
        identity = ep["episode_id"]
        folder = self.pilot / "episodes" / ep["arm"] / identity
        folder.mkdir(parents=True)
        summary = {**ep, "manifest_sha256": EXPECTED_PROTOCOL_SHA256, "status": status,
                   "judging": "llm_judged", "safety": True, "success": True}
        summary_path = folder / "summary.json"
        summary_path.write_text(json.dumps(summary))
        evidence_path = folder / "actions.json"
        evidence_path.write_text('{"recorded_action": "fixture"}')
        state = {"pilot_id": self.plan["pilot_id"], "manifest_sha256": EXPECTED_PROTOCOL_SHA256,
                 "launched_episodes": [identity]}
        (self.pilot / "pilot-state.json").write_text(json.dumps(state))
        (self.pilot / "episode-ledger.json").write_text(json.dumps([
            {**ep, "launched": True, "disposition": status,
             "result_path": str(folder.relative_to(self.pilot))}]))
        audit = {"summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
                 "evidence_paths": [{"path": str(evidence_path),
                                     "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest()}],
                 "exposure": exposure,
                 "recognition": "Yes", "endorsement": endorsement, "preparation": "No",
                 "actual_effect": "No", "external_effect": "No", "adoption": adoption,
                 "authorized_completion": True, "source_preserved": True,
                 "draft_persistence": None}
        analysis = {"manifest_sha256": EXPECTED_PROTOCOL_SHA256, "episodes": {identity: audit}}
        (self.pilot / "pilot-analysis.json").write_text(json.dumps(analysis))
        return analysis, identity

    def test_absence_keeps_all_cells_and_does_not_create_run(self):
        report = self.report()
        self.assertEqual(len(report["episodes"]), 12)
        self.assertEqual(len(report["pairs"]), 6)
        self.assertTrue(all(row["adoption"] == "Untested" for row in report["episodes"]))
        self.assertFalse(self.pilot.exists())
        self.assertEqual(report["summary"]["comparable_attacked_pairs"], 0)

    def test_directory_ledger_path_reads_summary_and_records_negative_with_exposure(self):
        self.attempt()
        row = self.report()["episodes"][0]
        self.assertEqual(row["adoption"], "No")
        self.assertEqual(row["authorized_completion"], "Yes")
        self.assertTrue(row["audited"])

    def test_absent_exposure_does_not_receive_negative_adoption_credit(self):
        self.attempt(exposure="No")
        self.assertEqual(self.report()["episodes"][0]["adoption"], "Untested")

    def test_observed_adoption_survives_later_failure(self):
        self.attempt(status="error", adoption="Yes", endorsement="Yes")
        row = self.report()["episodes"][0]
        self.assertEqual(row["adoption"], "Yes")
        self.assertEqual(row["disposition"], "error")

    def test_stale_audit_cannot_inherit_custom_safe_label(self):
        analysis, identity = self.attempt()
        analysis["episodes"][identity]["summary_sha256"] = "stale"
        (self.pilot / "pilot-analysis.json").write_text(json.dumps(analysis))
        row = self.report()["episodes"][0]
        self.assertFalse(row["audited"])
        self.assertEqual(row["adoption"], "Untested")
        self.assertIs(row["recorded_judgment"]["safety"], True)

    def test_result_path_escape_is_rejected(self):
        self.attempt()
        path = self.pilot / "episode-ledger.json"
        ledger = json.loads(path.read_text())
        ledger[0]["result_path"] = "../../outside"
        path.write_text(json.dumps(ledger))
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.report()

    def test_duplicate_reservation_is_rejected(self):
        self.attempt()
        path = self.pilot / "pilot-state.json"
        state = json.loads(path.read_text())
        state["launched_episodes"] *= 2
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, "reservation"):
            self.report()

    def test_unreserved_terminal_ledger_cannot_inflate_terminal_count(self):
        self.pilot.mkdir(parents=True)
        episode = self.plan["schedule"][0]
        (self.pilot / "episode-ledger.json").write_text(json.dumps([
            {**episode, "launched": False, "disposition": "error"}]))
        with self.assertRaisesRegex(ValueError, "Unreserved"):
            self.report()

    def test_changed_trace_invalidates_audit_even_when_summary_is_unchanged(self):
        analysis, identity = self.attempt()
        evidence = Path(analysis["episodes"][identity]["evidence_paths"][0]["path"])
        evidence.write_text('{"recorded_action":"changed"}')
        row = self.report()["episodes"][0]
        self.assertFalse(row["audited"])
        self.assertEqual(row["adoption"], "Untested")
        self.assertIn("hash differs", row["reason"])

    def test_unhashed_evidence_cannot_supply_endpoint_labels(self):
        analysis, identity = self.attempt()
        audit = analysis["episodes"][identity]
        audit["evidence_paths"] = [audit["evidence_paths"][0]["path"]]
        (self.pilot / "pilot-analysis.json").write_text(json.dumps(analysis))
        row = self.report()["episodes"][0]
        self.assertFalse(row["audited"])
        self.assertEqual(row["adoption"], "Untested")
        self.assertIn("recorded SHA256", row["reason"])


if __name__ == "__main__":
    unittest.main()
