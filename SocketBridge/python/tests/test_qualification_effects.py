import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("qualification_effects", Path(__file__).resolve().parents[3]/"scripts/verify_phase1_qualification.py")
effects = importlib.util.module_from_spec(spec); spec.loader.exec_module(effects)


class TestQualificationEffects(unittest.TestCase):
    def fixture(self, directory, combat=True, manual_after=True):
        def state(combat):
            return {"room_visit_id": "fight", "room": {"enemy_count": int(combat)},
                    "enemies": [{"can_shut_doors": True, "dead": False, "friendly": False, "hp": 1}] if combat else [],
                    "health": {"red_hearts": 6, "soul_hearts": 0}, "player": {"dead": False}}
        manual = {"kind": "game_message", "message": {"type": "CMD", "result": {"success": True, "mode": "MANUAL"}}}
        rows = [{"kind": "run_start"}]
        if not manual_after: rows.append(manual)
        rows += [{"kind": "game_message", "message": {"payload": {"COMBAT_FRAME": state(combat)}}},
                 {"kind": "safe_clear_confirmed"},
                 {"kind": "game_message", "message": {"payload": {"COMBAT_FRAME": state(False)}}}]
        if manual_after: rows.append(manual)
        (directory/"trace.jsonl").write_text("\n".join(json.dumps({"sequence": i, **r}) for i, r in enumerate(rows, 1)), encoding="utf-8")
        (directory/"report.json").write_text(json.dumps({"result": "CLEARED", "success": True, "contract_override_count": 0}))
        frozen = {"SocketBridge\\main.lua": "frozen-mod"}
        (directory/"manifest.json").write_text(json.dumps({"source_hashes": frozen}))
        (directory/"startup.json").write_text(json.dumps([{"phase": "prepared", "installed_mod_sha256": "frozen-mod", "restored_baseline": {"slot1": "frozen-save"}}]))
        return frozen

    def test_real_combat_and_post_clear_manual_ack_are_accepted(self):
        with tempfile.TemporaryDirectory() as name:
            p = Path(name); frozen = self.fixture(p)
            self.assertEqual(effects.verify_run(p, frozen)["issues"], [])

    def test_empty_room_cannot_be_counted_as_combat_success(self):
        with tempfile.TemporaryDirectory() as name:
            p = Path(name); frozen = self.fixture(p, combat=False)
            self.assertIn("NO_BLOCKING_COMBAT_OBSERVED", effects.verify_run(p, frozen)["issues"])

    def test_old_manual_ack_cannot_prove_cleanup(self):
        with tempfile.TemporaryDirectory() as name:
            p = Path(name); frozen = self.fixture(p, manual_after=False)
            self.assertIn("MANUAL_NOT_CONFIRMED_AFTER_CLEAR", effects.verify_run(p, frozen)["issues"])
