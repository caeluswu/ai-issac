import json
from pathlib import Path
import unittest
from combat.catalog import default_plan
from combat.controller import BuildResolver,JointActionSolver
from combat.models import WorldSnapshot
from combat.supervision import ProgressSupervisor
from tests.combat_fixtures import context


class TestFireCorridor(unittest.TestCase):
    def source(self):
        return WorldSnapshot.model_validate(json.loads((Path(__file__).parent/'fixtures/nest_fire_corridor.json').read_text(encoding='utf8')))

    def test_blocked_enemy_lane_can_target_a_tear_extinguishable_fire(self):
        s=self.source();b=BuildResolver().update(s)
        a=JointActionSolver().solve(s,default_plan(context(s)),b)
        self.assertTrue(a.target.startswith('terrain:fire:'),a)
        self.assertIsNotNone(a.diagnostics['route_goal'])
        self.assertNotEqual(a.shoot,(0,0))

    def test_blue_purple_and_white_fire_are_not_tear_targets(self):
        for variant in (2,3,4):
            s=self.source()
            for h in s.hazards:h['variant']=variant
            b=BuildResolver().update(s)
            a=JointActionSolver().solve(s,default_plan(context(s)),b)
            self.assertFalse((a.target or '').startswith('terrain:fire:'))

    def test_fire_damage_is_progress_but_a_new_fire_is_not(self):
        s=self.source();watcher=ProgressSupervisor();watcher.observe(s)
        t=s.model_copy(deep=True);t.frame+=1;t.hazards[0]['hp']-=1
        watcher.observe(t)
        self.assertEqual(watcher.last_progress,t.frame)
        self.assertEqual(watcher.evidence[0]['kind'],'fireplace_damaged_or_extinguished')
        u=t.model_copy(deep=True);u.frame+=1;u.hazards[0]['id']=999
        watcher.observe(u)
        self.assertEqual(watcher.last_progress,t.frame)
