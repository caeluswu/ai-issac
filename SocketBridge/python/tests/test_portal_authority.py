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
from combat.terrain_puzzle import TimedTerrainPuzzle
from tests.combat_fixtures import snapshot, context


class TestPortalAuthority(unittest.TestCase):
    def test_guard_is_not_a_solid_wall_and_does_not_block_shots(self):
        s = snapshot()
        g = GeometryService(s, allowed_portals=())
        start, end = Vec(x=500, y=160), Vec(x=526, y=160)
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
        self.assertTrue(solver.geometry.motion_clear(Vec(x=500, y=160), Vec(x=526, y=160)))
        self.assertFalse(solver.geometry.motion_clear(Vec(x=60, y=160), Vec(x=34, y=160)))
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

    def puzzle_entry(self):
        return WorldSnapshot.model_validate(json.loads((Path(__file__).parent/'fixtures/puzzle_portal_start.json').read_text(encoding='utf8')))

    def test_pit_lane_by_open_bottom_door_remains_reachable(self):
        s = WorldSnapshot.model_validate(json.loads((Path(__file__).parent/'fixtures/portal_nav_stall.json').read_text(encoding='utf8')))
        build = BuildResolver().update(s)
        nav = DoorNavigator(2, s.layout['doors']['2'])
        a = nav.step(s, default_plan(context(s)), build)
        self.assertTrue(nav.solver.geometry.path(s.player.pos, nav.goal))
        self.assertNotEqual(a.move,(0,0))
        self.assertFalse(a.diagnostics['no_safe_candidate'])
        self.assertEqual(nav.solver.geometry.allowed_portals,frozenset((2,)))

    def test_timed_room_native_entry_retreats_before_temporal_search(self):
        s = self.puzzle_entry(); s.player.pos.y = 150
        build = BuildResolver().update(s)
        nav = DoorNavigator(2, s.layout['doors']['2'])
        first = nav.step(s, default_plan(context(s)), build)
        self.assertEqual(first.diagnostics['puzzle_phase'], 'entry_retreat')
        self.assertGreater(first.move[1], 0)
        timed = tuple(int(i) for i,c in s.layout['grid'].items() if c['type'] == 9)
        g = GeometryService(s, timed_spikes=timed, allowed_portals=(2,))
        for _ in range(30):
            a = nav.step(s, default_plan(context(s)), build)
            if a.diagnostics['puzzle_phase'] != 'entry_retreat': break
            point, velocity, _, unsafe = PlayerMotion.step(s.player.pos, s.player.vel,
                PlayerMotion.desired(a.move,build), g, build)
            self.assertFalse(unsafe)
            s.player.pos, s.player.vel = point, velocity
            s.frame += 1
        self.assertIn(g.cell(s.player.pos), g.walkable)
        self.assertEqual(a.diagnostics['puzzle_phase'], 'cross')

    def test_entry_cannot_cross_an_unobserved_spike_and_has_deadline(self):
        s = self.puzzle_entry(); s.player.pos.y = 150
        build = BuildResolver().update(s)
        cell = s.layout['cells']['22']
        s.layout['grid']['22'] = dict(type=9,variant=0,state=0,collision=0,x=cell['x'],y=cell['y'])
        puzzle = TimedTerrainPuzzle(s.room_visit_id)
        # Walls on both sides leave only the unknown spike on the inward path.
        for index in ('21','23'):
            s.layout['cells'][index]['collision'] = 4
        a = puzzle.step(s,build,Vec(x=560,y=280),None,None,allowed_portals=(2,))
        self.assertEqual(a.move,(0,0))
        s.frame += 90
        with self.assertRaisesRegex(TimeoutError,'PUZZLE_ENTRY_RECOVERY_TIMEOUT'):
            puzzle.step(s,build,Vec(x=560,y=280),None,None,allowed_portals=(2,))


if __name__ == '__main__':
    unittest.main()
