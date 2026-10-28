import json
from pathlib import Path
import unittest
from combat.models import WorldSnapshot, Enemy, Vec
from combat.threats import ThreatField, EnemyAdapters
from combat.boundary_safety import boundary_danger
from combat.controller import JointActionSolver, BuildResolver
from combat.engagement import ClosedHost
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

    def test_closed_hosts_reposition_without_firing_then_yield_to_open_target(self):
        # Frame 17400 from the fourth-floor room that previously stalled.
        s = self.load("closed_hosts_room109.json")
        self.assertEqual(len(s.hostiles), 3)
        self.assertFalse(any(enemy.vulnerable for enemy in s.hostiles))
        solver = JointActionSolver()
        build, plan = BuildResolver().update(s), default_plan(context(s))
        action = solver.solve(s, plan, build)
        self.assertNotEqual(action.move, (0, 0))
        self.assertEqual(action.shoot, (0, 0))
        self.assertIsNone(action.target)
        self.assertEqual(action.diagnostics["engagement_reason"], "closed_host_firing_stance")
        self.assertIsNotNone(action.diagnostics["route_goal"])
        old_goal = action.diagnostics["route_goal"]

        s.frame += 15
        solver.recovery_level = 1
        action = solver.solve(s, plan, build)
        self.assertNotEqual(action.diagnostics["route_goal"], old_goal)
        self.assertEqual(action.shoot, (0, 0))

        opened_id = action.diagnostics["movement_target"]
        next(enemy for enemy in s.enemies if enemy.instance_id == opened_id).vulnerable = True
        s.frame += 1
        action = solver.solve(s, plan, build)
        self.assertEqual(action.diagnostics["movement_target"], opened_id)
        self.assertIsNone(action.diagnostics["engagement_reason"])

    def test_closed_host_engagement_adapter_can_be_replaced(self):
        class NoEngagement(ClosedHost):
            def route(self, *args): return None
        s = self.load("closed_hosts_room109.json")
        action = JointActionSolver(engagement_adapters={(27, 0): NoEngagement()}).solve(
            s, default_plan(context(s)), BuildResolver().update(s))
        self.assertIsNone(action.diagnostics["engagement_reason"])
        self.assertIsNone(action.diagnostics["movement_target"])
