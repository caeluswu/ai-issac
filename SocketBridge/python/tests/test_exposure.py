import json
from pathlib import Path
import unittest
from combat.catalog import default_plan
from combat.controller import BuildResolver, JointActionSolver
from combat.exposure import BuriedPin
from combat.geometry import GeometryService
from combat.models import WorldSnapshot
from tests.combat_fixtures import context


class TestBuriedPin(unittest.TestCase):
    def state(self):
        return WorldSnapshot.model_validate(json.loads((Path(__file__).parent/'fixtures/buried_pin_stall.json').read_text(encoding='utf8')))

    def test_captured_all_hidden_pose_repositions_instead_of_idle(self):
        s=self.state();build=BuildResolver().update(s);solver=JointActionSolver()
        a=solver.solve(s,default_plan(context(s)),build)
        self.assertNotEqual(a.move,(0,0))
        self.assertEqual(a.diagnostics['engagement_reason'],'buried_pin_exposure_probe')
        head=s.enemies[0];g=GeometryService(s,allowed_portals=())
        route=BuriedPin().route(g,s,head,build,None,None,())
        self.assertGreaterEqual(route.waypoints[-1].distance(head.pos),150)
        self.assertTrue(all(g.clear(p) for p in route.waypoints))
        for part in s.enemies[1:]:
            self.assertIsNone(BuriedPin().route(g,s,part,build,None,None,()))

    def test_exposed_head_returns_to_ordinary_attack_policy(self):
        s=self.state();s.enemies[0].vulnerable=True;s.enemies[0].collision_class=4
        a=JointActionSolver().solve(s,default_plan(context(s)),BuildResolver().update(s))
        self.assertIsNone(a.diagnostics['engagement_reason'])
        self.assertIsNotNone(a.target)


if __name__ == '__main__': unittest.main()
