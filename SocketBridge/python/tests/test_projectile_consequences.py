import json
from pathlib import Path
import unittest
from combat.models import WorldSnapshot
from combat.projectile_effects import ProjectileEffects
from combat.threats import ThreatField


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
