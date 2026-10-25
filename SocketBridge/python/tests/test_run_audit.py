"""Adversarial evidence checks independent of the controller's judge."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("phase1_audit", Path(__file__).resolve().parents[3]/"scripts/audit_phase1.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class TestAudit(unittest.TestCase):
    def fixture(self, directory, states, damage=0):
        report = {"damage_half_hearts": damage, "llm_configured": True, "success": True, "seed": "ABCD EFGH"}
        (directory/"report.json").write_text(json.dumps(report))
        (directory/"manifest.json").write_text("{}")
        rows = [{"kind": "run_start", "seed": "ABCD EFGH"},
                {"kind": "pause_confirmed", "entry_frame": 1, "paused_frame": 2},
                {"kind": "plan_installed", "source": "deepseek"}]
        for frame, (room, clear, hp) in enumerate(states):
            s = {"room_visit_id": room, "frame": frame, "paused": False,
                 "room": {"is_clear": clear, "enemy_count": 0 if clear else 1, "difficulty": 0},
                 "player": {"dead": hp == 0}, "stats": {"player_type": 0},
                 "health": {"red_hearts": hp, "soul_hearts": 0}, "enemies": []}
            rows.append({"kind": "game_message", "message": {"payload": {"COMBAT_FRAME": s}}})
        with (directory/"trace.jsonl").open("w") as f:
            for i, row in enumerate(rows, 1): f.write(json.dumps({"sequence": i, **row})+"\n")

    def test_three_successful_observations_and_door_transition(self):
        with tempfile.TemporaryDirectory() as name:
            p = Path(name)
            self.fixture(p, [("start", True, 6), ("fight", False, 6)]+[("fight", True, 6)]*3)
            self.assertEqual(audit.audit_run(p)["issues"], [])

    def test_interrupted_clear_signal_is_not_three_consecutive_observations(self):
        with tempfile.TemporaryDirectory() as name:
            p = Path(name)
            self.fixture(p, [("start", True, 6), ("fight", True, 6), ("fight", False, 6)]+[("fight", True, 6)]*2)
            self.assertIn("SUCCESS_WITHOUT_REQUIRED_EFFECT_EVIDENCE", audit.audit_run(p)["issues"])

    def test_entry_damage_cannot_disappear_from_report(self):
        with tempfile.TemporaryDirectory() as name:
            p = Path(name)
            self.fixture(p, [("start", True, 6), ("fight", False, 5)]+[("fight", True, 5)]*3)
            self.assertIn("DAMAGE_REPORT_MISMATCH", audit.audit_run(p)["issues"])

    def test_optimized_report_cannot_claim_settling_without_raw_evidence(self):
        with tempfile.TemporaryDirectory() as name:
            p = Path(name)
            self.fixture(p, [("start", True, 6), ("fight", False, 6)]+[("fight", True, 6)]*3)
            report = json.loads((p/"report.json").read_text()); report["safe_clear_ticks"] = 15
            (p/"report.json").write_text(json.dumps(report))
            self.assertIn("MISSING_SAFE_CLEAR_SETTLING", audit.audit_run(p)["issues"])
