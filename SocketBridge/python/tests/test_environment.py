import json
from pathlib import Path
import unittest
from combat.models import WorldSnapshot, Vec
from combat.environment import EnvironmentController
from combat.geometry import GeometryService
from combat.observation import SnapshotAssembler
from combat.threats import ThreatField
from combat.controller import JointActionSolver, BuildResolver
from combat.catalog import default_plan
from tests.combat_fixtures import context


class TestEnvironment(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((Path(__file__).parent/"fixtures/environment_tnt.json").read_text(encoding="utf8"))
        self.s = WorldSnapshot.model_validate(self.raw[0])

    def test_locked_empty_plate_room_is_not_combat(self):
        self.assertFalse(self.s.blocking_combat)
        self.assertFalse(self.s.room["is_clear"])
        self.assertEqual(EnvironmentController().objective(self.s).kind, "explosion_withdrawal")

    def test_unknown_fuse_and_tnt_chain_remain_dangerous_at_horizon(self):
        field = ThreatField(self.s)
        bomb = self.s.bombs[0]["pos"]
        self.assertLess(field.gap((bomb["x"], bomb["y"]), (bomb["x"], bomb["y"]), 40, 10), 0)
        self.assertTrue(any(t.source == "potential_tnt_chain" for t in field.threats))
        self.s.bombs = []
        self.assertFalse(any(t.source == "potential_tnt_chain" for t in ThreatField(self.s).threats))

    def test_controller_withdraws_from_existing_blast_overlap(self):
        a = JointActionSolver().solve(self.s, default_plan(context(self.s)), BuildResolver().update(self.s), True)
        self.assertNotEqual(a.move, (0, 0))
        self.assertGreater(a.move[1], 0)
        self.assertEqual(a.shoot, (0, 0))

    def test_knockback_boundary_can_move_out_but_not_deeper(self):
        s = WorldSnapshot.model_validate(self.raw[1]); g = GeometryService(s)
        origin = s.player.pos
        p, _, _, unsafe = g.sweep(origin, Vec(x=3, y=0))
        self.assertGreater(p.x, origin.x); self.assertFalse(unsafe)
        p, _, _, _ = g.sweep(origin, Vec(x=-3, y=0))
        self.assertEqual(p.x, origin.x)

    def test_plate_permission_is_local_and_does_not_unlock_spikes(self):
        self.s.bombs = []
        for index, grid in self.s.layout["grid"].items():
            if grid["type"] == 12:
                grid.update(collision=0, state=4)
                self.s.layout["cells"][index]["collision"] = 0
        objective = EnvironmentController().objective(self.s)
        self.assertEqual(objective.kind, "activate_pressure_plate")
        self.assertFalse(GeometryService(self.s).clear(objective.goal))
        self.assertTrue(GeometryService(self.s, objective.allowed_devices).clear(objective.goal))
        self.s.layout["grid"]["22"]["type"] = 8
        self.assertFalse(GeometryService(self.s, objective.allowed_devices).clear(objective.goal))

    def test_destroyed_tnt_retains_short_transition_hazard(self):
        assembler = SnapshotAssembler()
        assembler.consume({"payload": {"COMBAT_FRAME": self.raw[0]}})
        nxt = json.loads(json.dumps(self.raw[0])); nxt["frame"] += 1
        nxt["layout"]["grid"]["18"]["state"] = 4; nxt["layout"]["grid"]["18"]["collision"] = 0
        s = assembler.consume({"payload": {"COMBAT_FRAME": nxt}})
        self.assertEqual(s.environment_residuals[0]["id"], "18")
        nxt["frame"] += 7
        s = assembler.consume({"payload": {"COMBAT_FRAME": nxt}})
        self.assertEqual(s.environment_residuals, [])
