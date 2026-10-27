import json
from pathlib import Path
import unittest
from combat.models import WorldSnapshot, Enemy, Vec
from combat.threats import ThreatField, EnemyAdapters
from combat.boundary_safety import boundary_danger
from combat.controller import JointActionSolver, BuildResolver
from combat.catalog import default_plan
from tests.combat_fixtures import context, snapshot


class TestRecordedEnemyEffects(unittest.TestCase):
    def load(self, name):
        return WorldSnapshot.model_validate(json.loads((Path(__file__).parent/"fixtures"/name).read_text(encoding="utf8")))

    def test_airborne_explosive_spider_retains_landing_forecast(self):
        s = self.load("airborne_spider.json")
        self.assertEqual(s.enemies[0].collision_class, 0)
        field = ThreatField(s)
        self.assertTrue(any(t.source == "uncalibrated_landing_envelope" for t in field.threats))
        solver = JointActionSolver()
        action = solver.solve(s, default_plan(context(s)), BuildResolver().update(s))
        # The captured choice ran northwest into an airborne spider and claimed
        # zero risk until actual landing contact. That cannot remain "safe".
        prior = [c for c in solver.last_candidates if c["move"] == (-1, -1)]
        self.assertTrue(prior)
        self.assertFalse(any(c["safe"] for c in prior))

    def test_mulliboom_variant_has_attack_and_residual_death_envelopes(self):
        s = self.load("mulliboom_death.json")
        target = next(e for e in s.hostiles if (e.type, e.variant) == (16, 2))
        field = ThreatField(s)
        self.assertTrue(any(t.source == "possible_contact_triggered_explosion" for t in field.threats))
        self.assertEqual(EnemyAdapters().resolve(target).death_radius(target), 100)
        self.assertFalse(field.attack_safe(target, BuildResolver().update(s), [target.pos]*13, 1))
        s.recent_deaths = [{"instance_id": target.instance_id, "type": 16, "variant": 2,
                            "pos": target.pos.model_dump(), "frame": s.frame}]
        self.assertTrue(any(t.identity == "death:"+target.instance_id for t in ThreatField(s).threats))
        target.variant = 0
        self.assertEqual(EnemyAdapters().resolve(target).death_radius(target), 0)

    def test_little_horn_moving_pitfall_is_forecast_despite_zero_hp(self):
        s=snapshot(enemies=False)
        s.player.pos=Vec(x=401,y=319)
        pit=Enemy(instance_id="pitfall:observed",type=291,variant=2,
                  pos=Vec(x=541,y=320),vel=Vec(x=-8.7,y=0),hp=0,max_hp=0,
                  collision_radius=30,vulnerable=False,friendly=False,dead=False,
                  collision_class=0,contact_damage=0,can_shut_doors=False)
        s.enemies=[pit]
        self.assertEqual(s.hostiles,[])
        field=ThreatField(s)
        self.assertTrue(any(t.source=="moving_pitfall_observed_motion" for t in field.threats))
        self.assertLess(field.gap((401,319),(401,319),15,10),0)
        pit.collision_class=3;pit.pos=Vec(x=407,y=319)
        self.assertTrue(boundary_danger(s))
