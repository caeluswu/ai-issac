import json
from pathlib import Path
import unittest
from combat.catalog import default_plan
from combat.controller import BuildResolver,JointActionSolver
from combat.geometry import GeometryService
from combat.models import WorldSnapshot,Vec
from tests.combat_fixtures import context


class TestTargetVolume(unittest.TestCase):
    def setup_case(self):
        s=WorldSnapshot.model_validate(json.loads((Path(__file__).parent/'fixtures/mom_wall_ray.json').read_text(encoding='utf8')))
        target=next(e for e in s.enemies if e.type==45 and e.variant==0 and e.vulnerable
            and abs(e.pos.y-s.player.pos.y)<4 and 120<abs(e.pos.x-s.player.pos.x)<280)
        build=BuildResolver().update(s);solver=JointActionSolver()
        p=default_plan(context(s)).profiles['safe']
        shot=(1 if target.pos.x>s.player.pos.x else -1,0)
        return s,target,build,solver,p,shot

    def test_visible_part_can_be_hit_before_the_wall_embedded_center(self):
        s,e,b,solver,p,shot=self.setup_case();g=GeometryService(s)
        self.assertFalse(g.line_clear(s.player.pos,e.pos,shot=True))
        self.assertGreater(solver.hit_score(s,b,p,e,shot,Vec(x=0,y=0),g),0)
        self.assertTrue(g.firing_route(s.player.pos,e.pos,b.effective_range,target_radius=e.collision_radius+5))

    def test_first_contact_does_not_allow_shooting_through_a_blocker(self):
        s,e,b,solver,p,shot=self.setup_case();g=GeometryService(s)
        index=str(g.cell(Vec(x=(s.player.pos.x+e.pos.x)/2,y=e.pos.y)))
        s.layout['cells'][index]['collision']=4
        self.assertEqual(solver.hit_score(s,b,p,e,shot,Vec(x=0,y=0),GeometryService(s)),0)

    def test_enemy_entirely_in_the_wall_does_not_get_a_fake_lane(self):
        s,e,b,solver,p,shot=self.setup_case();e.collision_radius=1
        self.assertEqual(solver.hit_score(s,b,p,e,shot,Vec(x=0,y=0),GeometryService(s)),0)


if __name__ == '__main__':unittest.main()
