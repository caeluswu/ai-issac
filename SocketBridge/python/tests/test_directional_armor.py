import unittest
from combat.catalog import default_plan
from combat.controller import BuildResolver, JointActionSolver
from combat.directional_armor import DirectionalArmor, damage_from_velocity, SAMPLES
from combat.geometry import GeometryService
from combat.models import Vec
from tests.combat_fixtures import snapshot, context


class TestDirectionalArmor(unittest.TestCase):
    def state(self):
        s = snapshot(); e = s.enemies[0]
        e.type = 41
        e.damage_directions = {key:(x == -1 and y == 0) for x,y,key,_ in SAMPLES}
        return s

    def test_normal_tears_do_not_reward_the_armored_face(self):
        s = self.state(); solver = JointActionSolver(); build = BuildResolver().update(s)
        plan = default_plan(context(s)); p = plan.profiles[plan.default_profile]
        self.assertEqual(solver.hit_score(s,build,p,s.enemies[0],(1,0),Vec(x=0,y=0),GeometryService(s)),0)
        s.enemies[0].damage_directions = None
        self.assertGreater(solver.hit_score(s,build,p,s.enemies[0],(1,0),Vec(x=0,y=0),GeometryService(s)),0)

    def test_flank_route_reaches_a_permitted_ray_without_crossing_body(self):
        s = self.state(); e = s.enemies[0]; g = GeometryService(s,allowed_portals=())
        build = BuildResolver().update(s)
        route = DirectionalArmor().route(g,s,e,build,None,None,())
        self.assertIsNotNone(route)
        self.assertIs(damage_from_velocity(e,e.pos-route.waypoints[-1]),True)
        for p in route.waypoints:
            self.assertGreater(p.distance(e.pos),e.collision_radius+s.player.collision_radius+8)
        a = JointActionSolver().solve(s,default_plan(context(s)),build)
        self.assertEqual(a.diagnostics['engagement_reason'],'directional_armor_flank')
        self.assertNotEqual(a.shoot,(1,0))

    def test_missing_samples_and_knife_are_not_claimed_as_supported(self):
        s = self.state();e = s.enemies[0]
        e.damage_directions = {'1,0':False}
        self.assertIsNone(damage_from_velocity(e,Vec(x=-10,y=0)))
        build = BuildResolver().update(s)
        self.assertIsNone(DirectionalArmor().route(GeometryService(s),s,e,build,None,None,()))
        e.damage_directions = {key:(x == -1) for x,y,key,_ in SAMPLES}
        self.assertIsNone(DirectionalArmor().route(GeometryService(s),s,e,
            build.model_copy(update={'family':'knife'}),None,None,()))
