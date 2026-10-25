"""Wall contact should slide; damage terrain must never become a fake wall."""
import json
from pathlib import Path
import unittest
from combat.models import Vec, WorldSnapshot
from combat.geometry import GeometryService
from combat.controller import JointActionSolver, BuildResolver
from combat.catalog import default_plan
from tests.combat_fixtures import snapshot, context


class TestCollisionMotion(unittest.TestCase):
    def test_solid_wall_stops_one_axis_and_allows_sliding(self):
        g = GeometryService(snapshot())
        p, v, blocked, unsafe = g.sweep(Vec(x=33, y=160), Vec(x=-4, y=3))
        self.assertEqual(p, Vec(x=33, y=163))
        self.assertEqual(v, Vec(x=0, y=3))
        self.assertTrue(blocked); self.assertFalse(unsafe)

    def test_spike_is_unsafe_not_a_safe_collision_stop(self):
        s = snapshot(); s.layout["grid"]["63"] = {"type": 8, "variant": 0, "collision": 0, "x": 120, "y": 160}
        _, _, _, unsafe = GeometryService(s).sweep(Vec(x=160, y=160), Vec(x=-12, y=0))
        self.assertTrue(unsafe)

    def test_pit_blocks_walking_but_not_flight(self):
        s = snapshot(); s.layout["cells"]["63"]["collision"] = 1
        _, _, blocked, unsafe = GeometryService(s).sweep(Vec(x=160, y=160), Vec(x=-12, y=0))
        self.assertTrue(blocked); self.assertFalse(unsafe)
        s.stats["can_fly"] = True
        _, _, blocked, unsafe = GeometryService(s).sweep(Vec(x=160, y=160), Vec(x=-12, y=0))
        self.assertFalse(blocked); self.assertFalse(unsafe)

    def test_navigation_margin_does_not_pin_physical_wall_contact(self):
        g = GeometryService(snapshot())
        position = Vec(x=30.001, y=160)
        self.assertFalse(g.clear(position))
        moved, _, _, unsafe = g.sweep(position, Vec(x=3, y=0))
        self.assertGreater(moved.x, position.x)
        self.assertFalse(unsafe)

    def test_captured_crowd_trap_keeps_wall_slide_escape(self):
        s = WorldSnapshot.model_validate(json.loads((Path(__file__).parent/"fixtures/crowd_wall.json").read_text()))
        a = JointActionSolver().solve(s, default_plan(context(s)), BuildResolver().update(s))
        self.assertNotEqual(a.move, (0, 0))
        # The recorded stationary choice had 27.68 early penetration. A wall
        # slide must improve immediate clearance even if later uncertainty grows.
        self.assertLess(a.score["early_risk"], 25)
        self.assertGreater(a.diagnostics["blocked_steps"], 0)
