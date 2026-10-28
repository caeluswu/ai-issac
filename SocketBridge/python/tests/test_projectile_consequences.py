import json
from pathlib import Path
import unittest
from combat.models import WorldSnapshot
from combat.projectile_effects import ProjectileEffects
from combat.threats import ThreatField
from tests.combat_fixtures import snapshot


class TestLandingConsequences(unittest.TestCase):
    def test_captured_blood_projectile_predicts_unsafe_landing_before_creep_exists(self):
        fixture=json.loads((Path(__file__).parent/"fixtures/horseman_landing.json").read_text(encoding="utf-8"))
        before,near,after=map(WorldSnapshot.model_validate,fixture["snapshots"])
        self.assertFalse(any(h.get("variant")==22 for h in near.hazards))
        self.assertTrue(any(h.get("variant")==22 for h in after.hazards))
        p=near.projectiles["enemy_projectiles"][0]
        forecast=ProjectileEffects().forecast(p,near)
        self.assertEqual(len(forecast),1)
        self.assertEqual(forecast[0].radius,60)
        player=(near.player.pos.x,near.player.pos.y)
        self.assertLess(ThreatField(near).gap(player,player,1,near.player.collision_radius+8),0)
        unknown={**p,"owner_type":10}
        self.assertEqual(ProjectileEffects().forecast(unknown,near),[])

    def test_gurgle_falling_shot_excludes_impact_not_entire_flight(self):
        # From J835 run-mainline-20261027T162335916799Z frame 9492.
        # The shot landed at frame 9496 and the player lost one half-heart.
        s=snapshot()
        p={"instance_id":"3812852525:179","id":179,"owner_type":87,"variant":0,
           "pos":{"x":279,"y":160},"vel":{"x":-4.1,"y":0.2},
           "collision_radius":5,"height":-20,"falling_speed":5.7,"falling_accel":0.5}
        s.projectiles["enemy_projectiles"]=[p]
        impact=ProjectileEffects().forecast(p,s)[0]
        self.assertEqual(impact.source,"j835_gurgle_falling_impact_bound")
        self.assertGreater(impact.start,0)
        self.assertLess(impact.start,6)
        self.assertLess(impact.gap((216,162),(216,162),4,10),0)
        self.assertGreater(impact.gap((100,300),(100,300),4,10),0)
        self.assertGreater(impact.gap((216,162),(216,162),0,10),0)
        self.assertTrue(any(t.source==impact.source for t in ThreatField(s).threats))
        self.assertEqual(ProjectileEffects().forecast({**p,"owner_type":10},s),[])

    def test_gurgle_unknown_descent_does_not_invent_impact(self):
        p={"id":1,"owner_type":87,"variant":0,"pos":{"x":100,"y":100},
           "vel":{"x":0,"y":1},"height":-40,"falling_speed":-2,"falling_accel":0.5}
        self.assertEqual(ProjectileEffects().forecast(p,snapshot()),[])
