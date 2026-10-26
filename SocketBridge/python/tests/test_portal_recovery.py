import json
from pathlib import Path
import unittest
from combat.models import WorldSnapshot,Vec
from combat.geometry import GeometryService
from combat.controller import JointActionSolver,BuildResolver
from combat.catalog import default_plan
from tests.combat_fixtures import context


class TestPortalRecovery(unittest.TestCase):
    def state(self):
        return WorldSnapshot.model_validate(json.loads((Path(__file__).parent/'fixtures/portal_bomb_recovery.json').read_text(encoding='utf8')))

    def test_observed_open_aperture_allows_only_inward_retreat(self):
        s=self.state();g=GeometryService(s)
        self.assertNotIn(g.cell(s.player.pos),g.walkable)
        self.assertTrue(g.motion_clear(s.player.pos,s.player.pos+Vec(x=-4,y=0)))
        self.assertFalse(g.motion_clear(s.player.pos,s.player.pos+Vec(x=4,y=0)))
        s.layout['doors']['2']['is_open']=False
        self.assertFalse(GeometryService(s).motion_clear(s.player.pos,s.player.pos+Vec(x=-4,y=0)))

    def test_bomb_escape_does_not_hold_still_outside_the_interior_grid(self):
        s=self.state();solver=JointActionSolver()
        action=solver.solve(s,default_plan(context(s)),BuildResolver().update(s))
        self.assertLess(action.move[0],0)
        self.assertFalse(action.diagnostics['invalid_pose_stall'])
        self.assertTrue(any(c['invalid_pose_stall'] for c in solver.last_candidates if c['move']==(0,0)))

    def test_door_opening_invalidates_geometry_for_inward_recovery(self):
        s=self.state();s.layout['doors']['2']['is_open']=False;solver=JointActionSolver();build=BuildResolver().update(s);plan=default_plan(context(s))
        solver.solve(s,plan,build);before=solver.geometry
        s.layout['doors']['2']['is_open']=True;s.frame+=1
        solver.solve(s,plan,build)
        self.assertIsNot(solver.geometry,before)
        self.assertIn(2,[slot for slot,_ in solver.geometry.portals])

