import json
from pathlib import Path
import unittest
from combat.models import WorldSnapshot, Vec
from combat.geometry import GeometryService
from combat.threats import ThreatField
from combat.controller import JointActionSolver, BuildResolver
from combat.navigation import DoorNavigator
from combat.supervision import ProgressSupervisor
from combat.catalog import default_plan
from tests.combat_fixtures import context, snapshot


class TestExposedFailures(unittest.TestCase):
    def load(self, index):
        return WorldSnapshot.model_validate(json.loads((Path(__file__).parent/f"fixtures/holdout_failure_{index:02d}.json").read_text(encoding="utf8")))

    def test_contact_zero_body_is_not_a_damage_field(self):
        s = self.load(3)
        body = next(e for e in s.hostiles if e.type == 302)
        self.assertEqual(body.contact_damage, 0)
        self.assertFalse(any(t.identity.startswith(body.instance_id) for t in ThreatField(s).threats))
        body.contact_damage = 1
        self.assertTrue(any(t.identity.startswith(body.instance_id) for t in ThreatField(s).threats))

    def test_occluded_wall_target_causes_stable_lane_relocation(self):
        s = self.load(16); g = GeometryService(s); b = BuildResolver().update(s)
        self.assertEqual(g.firing_route(s.player.pos, s.enemies[0].pos, b.effective_range), [])
        solver = JointActionSolver(); p = default_plan(context(s))
        action = solver.solve(s, p, b)
        self.assertTrue(action.diagnostics["occlusion_reposition"])
        self.assertNotEqual(action.move, (0, 0))
        goal = action.diagnostics["route_goal"]
        s.frame += 1; s.player.pos.y -= 8
        self.assertEqual(solver.solve(s, p, b).diagnostics["route_goal"], goal)

    def test_fire_room_navigation_avoids_live_fire_cells(self):
        s = self.load(44); s.player.dead = False; s.health["red_hearts"] = 6
        s.player.pos = Vec(x=320, y=400); s.player.vel = Vec(x=0, y=0)
        g = GeometryService(s)
        for fire in s.hazards:
            self.assertNotIn(g.cell(Vec(**fire["pos"])), g.walkable)
        navigator = DoorNavigator(1, s.layout["doors"]["1"])
        action = navigator.step(s, default_plan(context(s)), BuildResolver().update(s))
        self.assertNotEqual(action.move[0], 0)
        self.assertEqual(action.shoot, (0, 0))

    def test_open_portal_crossing_is_explicit_and_does_not_open_closed_doors(self):
        s = snapshot(enemies=False); s.player.pos = Vec(x=530, y=160)
        navigator = DoorNavigator(2, s.layout["doors"]["2"])
        b, p = BuildResolver().update(s), default_plan(context(s))
        action = navigator.step(s, p, b)
        self.assertEqual(action.state, "NAVIGATE_PORTAL")
        self.assertEqual(action.move, (1, 0))
        s.layout["doors"]["2"]["is_open"] = False
        self.assertNotEqual(navigator.step(s, p, b).state, "NAVIGATE_PORTAL")

    def test_clear_room_navigation_cannot_disable_progress_watchdog(self):
        s = snapshot(enemies=False)
        watch = ProgressSupervisor(); watch.observe(s, require_progress=True)
        s = s.model_copy(deep=True); s.frame += 240
        self.assertEqual(watch.observe(s, require_progress=True), "STALL_UNRECOVERED")
