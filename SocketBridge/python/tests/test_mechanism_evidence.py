import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location("mechanism_live_evidence",Path(__file__).resolve().parents[3]/"scripts/validate_mechanism_live.py")
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class TestMechanismEvidence(unittest.TestCase):
    def test_preinitialization_and_other_room_feedback_cannot_count_as_effects(self):
        def frame(room,action,tear):
            return {"kind":"game_message","message":{"payload":{"COMBAT_FRAME":{"frame":101,"room_visit_id":"new",
                "weapon_feedback":{"history":[{"frame":100,"room_visit_id":room,"action_id":action,"consistent":True,
                    "queried":dict.fromkeys(("left","right","up","down"),True),"values":dict.fromkeys(("left","right","up","down"),False)}]},
                "projectiles":{"player_tears":[{"instance_id":tear,"owner_type":1,"entity_frame_count":0,"collision_damage":14,"vel":{"x":10,"y":0}}]}}}}}
        def action(identity,reason):
            return {"kind":"action_sent","action_id":identity,"shoot":[0,0],"metadata":{"diagnostics":{"fire_reason":reason,"mechanism":{"program_sha256":"fixture"}}}}
        rows=[action(1,"program_release"),frame("new",1,"before_init"),{"kind":"mechanism_initialization_complete","frame":100},
              action(2,"entry_move_only"),action(3,"program_release"),{"kind":"game_message","message":{"type":"ACTION_RECEIPT","accepted":True,"action_id":3}},
              frame("old",3,"new_tear")]
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/"trace.jsonl").write_text(''.join(json.dumps(r)+'\n' for r in rows))
            proof=module.proof_from_trace(p,"fixture")
            self.assertEqual(proof["adopted_actions"],2);self.assertEqual(proof["effective_program_actions"],1)
            self.assertEqual(proof["new_tears_observed"],1);self.assertEqual(proof["applied_releases"],0)

    def test_no_initialization_marker_produces_no_combat_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/"trace.jsonl").write_text('{"kind":"action_sent","action_id":1}\n')
            proof=module.proof_from_trace(p,"fixture")
            self.assertFalse(proof["initialization_observed"]);self.assertEqual(proof["adopted_actions"],0)
