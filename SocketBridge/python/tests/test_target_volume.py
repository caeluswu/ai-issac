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

    def test_range_ends_at_the_first_contact_not_the_hidden_center(self):
        s,e,b,solver,p,shot=self.setup_case();distance=e.pos.distance(s.player.pos)
        reachable=b.model_copy(update={'effective_range':distance-30})
        self.assertGreater(solver.hit_score(s,reachable,p,e,shot,Vec(x=0,y=0),GeometryService(s)),0)
        unreachable=b.model_copy(update={'effective_range':distance-70})
        self.assertEqual(solver.hit_score(s,unreachable,p,e,shot,Vec(x=0,y=0),GeometryService(s)),0)

    def test_mom_closed_wall_prepares_a_lane_without_entering_hand_range(self):
        s,_,b,solver,_,_=self.setup_case()
        s.player.pos=Vec(x=240,y=240);s.player.vel=Vec(x=0,y=0)
        self.assertTrue(GeometryService(s).clear(s.player.pos))
        s.enemies=[e for e in s.enemies if e.type==45]
        s.projectiles['enemy_projectiles']=[]
        for e in s.enemies:
            e.vulnerable=False;e.state=3;e.collision_class=0;e.vel=Vec(x=0,y=0)
        a=solver.solve(s,default_plan(context(s)),b)
        self.assertEqual(a.diagnostics['engagement_reason'],'mom_wall_firing_stance')
        self.assertNotEqual(a.move,(0,0))
        self.assertEqual(a.shoot,(0,0))
        route=solver.engagement.choose(solver.geometry,s,b,solver.modules['position'],solver.modules['fire'])
        self.assertGreaterEqual(route.waypoints[-1].distance(route.enemy.pos),140)

    def test_escape_rollout_does_not_return_to_a_dangerous_firing_lane(self):
        s=WorldSnapshot.model_validate(json.loads((Path(__file__).parent/'fixtures/fast_mom_shadow.json').read_text(encoding='utf8')))
        solver=JointActionSolver();a=solver.solve(s,default_plan(context(s)),BuildResolver().update(s))
        self.assertTrue(a.diagnostics['route_follow_suspended_for_threat'])
        self.assertNotEqual(a.move,(0,0))
        chosen=next(c for c in solver.last_candidates if c['move']==a.move and c['shoot']==a.shoot)
        self.assertGreater(chosen['escape_directions'],0)


if __name__ == '__main__':unittest.main()
