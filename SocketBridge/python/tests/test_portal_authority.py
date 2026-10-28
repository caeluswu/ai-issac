"""Ordinary actions cannot enter the automatic door-pull region."""
import json
from pathlib import Path
import unittest

from combat.catalog import default_plan
from combat.controller import BuildResolver, JointActionSolver
from combat.geometry import GeometryService
from combat.models import Vec, WorldSnapshot
from combat.motion import PlayerMotion
from combat.navigation import DoorNavigator
from tests.combat_fixtures import snapshot, context


class TestPortalAuthority(unittest.TestCase):
    def test_guard_is_not_a_solid_wall_and_does_not_block_shots(self):
        s = snapshot()
        g = GeometryService(s, allowed_portals=())
        start, end = Vec(x=490, y=160), Vec(x=510, y=160)
        self.assertFalse(g.motion_clear(start, end))
        self.assertTrue(g.sweep(start, end-start)[3])
        self.assertTrue(g.line_clear(start, end, shot=True))
        self.assertTrue(GeometryService(s, allowed_portals=(2,)).motion_clear(start, end))

    def test_entry_can_retreat_inward_but_not_slide_toward_door(self):
        g = GeometryService(snapshot(), allowed_portals=())
        start = Vec(x=530, y=160)
        self.assertTrue(g.motion_clear(start, Vec(x=525, y=160)))
        self.assertFalse(g.motion_clear(start, Vec(x=535, y=160)))
        self.assertFalse(g.motion_clear(start, Vec(x=530, y=165)))
        s = snapshot(enemies=False)
        s.player.pos = start
        a = JointActionSolver().solve(s, default_plan(context(s)), BuildResolver().update(s), True)
        self.assertLess(a.move[0], 0)

    def test_only_selected_portal_is_authorized_and_cache_changes(self):
        s = snapshot(enemies=False)
        s.layout['doors']['0'] = dict(s.layout['doors']['2'], x=0)
        solver = JointActionSolver(); build = BuildResolver().update(s)
        plan = default_plan(context(s))
        solver.solve(s, plan, build, True)
        original = solver.geometry
        solver.solve(s, plan, build, True, allowed_portals=(2,))
        self.assertIsNot(solver.geometry, original)
        self.assertTrue(solver.geometry.motion_clear(Vec(x=490, y=160), Vec(x=510, y=160)))
        self.assertFalse(solver.geometry.motion_clear(Vec(x=70, y=160), Vec(x=50, y=160)))
        s.player.pos = Vec(x=525, y=160)
        a = DoorNavigator(2, s.layout['doors']['2']).step(s, plan, build)
        self.assertEqual(a.move, (1, 0))
        self.assertEqual(a.state, 'NAVIGATE_PORTAL')

    def test_captured_bottom_door_pull_rejects_lateral_entry(self):
        s = WorldSnapshot.model_validate(json.loads((Path(__file__).parent/'fixtures/open_door_pull.json').read_text(encoding='utf8')))
        build = BuildResolver().update(s)
        g = GeometryService(s, allowed_portals=())
        _, _, _, unsafe = PlayerMotion.step(s.player.pos, s.player.vel,
            PlayerMotion.desired((1, 0), build), g, build)
        self.assertTrue(unsafe)
        solver = JointActionSolver()
        a = solver.solve(s, default_plan(context(s)), build)
        self.assertLess(a.move[1], 0)
        chosen = next(c for c in solver.last_candidates if c['move'] == a.move and c['shoot'] == a.shoot)
        self.assertFalse(chosen['wall'])
        self.assertLess(chosen['predicted_next_position'][1], s.player.pos.y)


if __name__ == '__main__':
    unittest.main()
