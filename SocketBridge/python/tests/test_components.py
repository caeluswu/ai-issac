import json
from pathlib import Path
import unittest
from combat.catalog import default_plan, compile_plan
from combat.components import EXECUTABLES, Registry, AttackProposal, CapabilitySet
from combat.controller import JointActionSolver, BuildResolver
from combat.models import WorldSnapshot, Vec
from combat.threats import EnemyAdapters, GenericEnemy, ThreatField, ThreatForecast
from tests.combat_fixtures import snapshot, context


class TestReplaceableComponents(unittest.TestCase):
    def test_component_fault_uses_declared_substitute_and_records_cause(self):
        class BrokenFire:
            def __init__(self, params): pass
            def propose(self, *args): raise RuntimeError("fixture fault")
        registry = Registry(); registry.factories.update(EXECUTABLES.factories)
        registry.factories["fire.normal_tears"] = BrokenFire
        s = snapshot(); solver = JointActionSolver(registry=registry)
        action = solver.solve(s, default_plan(context(s)), BuildResolver().update(s))
        self.assertNotEqual(action.shoot, (0, 0))
        self.assertEqual(action.diagnostics["component_failures"][0]["method"], "propose")
        self.assertTrue(solver.modules["fire"].broken)

    def test_stateful_weapon_updates_once_per_frame_not_per_candidate(self):
        class ChargeFixture:
            def __init__(self, params): self.updates = 0; self.last = None
            def begin_frame(self, s, build):
                if self.last != s.frame: self.updates += 1; self.last = s.frame
            def propose(self, *args):
                return [AttackProposal((0, 1) if self.updates >= 3 else (0, 0), .9, 1., "fixture")]
        registry = Registry(); registry.factories.update(EXECUTABLES.factories)
        registry.factories["fire.normal_tears"] = ChargeFixture
        s = snapshot(); solver = JointActionSolver(registry=registry)
        plan, build = default_plan(context(s)), BuildResolver().update(s)
        for frame in range(3):
            s.frame = frame
            a = solver.solve(s, plan, build)
            self.assertEqual(solver.modules["fire"].updates, frame+1)
            self.assertEqual(a.shoot, (0, 0) if frame < 2 else (0, 1))

    def test_weapon_replacement_does_not_change_scheduler(self):
        class FixtureCharge:
            capabilities = CapabilitySet("fixture_charge")
            def __init__(self, params): pass
            def propose(self, s, build, profile, target, velocity, geometry, aim=None):
                return [AttackProposal((0, -1), .9, 1., "fixture_charge_release")]
        registry = Registry(); registry.factories.update(EXECUTABLES.factories)
        registry.factories["fire.normal_tears"] = FixtureCharge
        s = snapshot(); a = JointActionSolver(registry=registry).solve(s, default_plan(context(s)), BuildResolver().update(s))
        self.assertEqual(a.shoot, (0, -1))
        self.assertEqual(a.diagnostics["fire_reason"], "fixture_charge_release")

    def test_enemy_forecast_replacement_does_not_change_scheduler(self):
        class HarmlessFixture(GenericEnemy):
            def forecast(self, enemy, snapshot): return []
        adapters = EnemyAdapters(); adapters.register(10, 0, HarmlessFixture())
        self.assertEqual(ThreatField(snapshot(), adapters).threats, [])

    def test_legacy_plan_is_explicitly_upgraded(self):
        s = snapshot(); plan = default_plan(context(s)).model_dump()
        plan["schema_version"] = 1; plan.pop("required_capabilities")
        upgraded = compile_plan(plan, context(s))
        self.assertEqual(upgraded.schema_version, 2)
        self.assertEqual(upgraded.required_capabilities, ["normal_tears"])

    def test_live_rock_failure_snapshot_has_progress_action(self):
        s = WorldSnapshot.model_validate(json.loads((Path(__file__).parent/"fixtures/rock_stall.json").read_text()))
        solver = JointActionSolver(); solver.recovery_level = 2
        a = solver.solve(s, default_plan(context(s)), BuildResolver().update(s))
        self.assertNotEqual(a.move, (0, 0))
        self.assertTrue(a.diagnostics["route_goal"])


class TestThreats(unittest.TestCase):
    def test_fast_distant_projectile_is_not_pruned_by_nearness(self):
        s = snapshot(); s.enemies = []
        s.projectiles["enemy_projectiles"] = [{"pos": {"x": 1000, "y": 160}, "vel": {"x": -80, "y": 0}, "collision_radius": 4}]
        field = ThreatField(s)
        self.assertLess(field.gap((280, 160), (280, 160), 9, 10), 0)

    def test_bomb_only_dangerous_during_explosion_window(self):
        s = snapshot(); s.enemies = []
        s.bombs = [{"id": 1, "pos": {"x": 280, "y": 160}, "timer": 30, "explosion_radius": 100}]
        field = ThreatField(s)
        self.assertGreater(field.gap((280, 160), (280, 160), 1, 10), 0)
        self.assertLess(field.gap((280, 160), (280, 160), 30, 10), 0)

    def test_lethal_boom_fly_attack_requires_withdrawal(self):
        s = snapshot(); e = s.enemies[0]; e.type = 25; e.hp = 1
        field = ThreatField(s); build = BuildResolver().update(s)
        self.assertFalse(field.attack_safe(e, build, [e.pos]*13, 1))
        self.assertTrue(field.attack_safe(e, build, [Vec(x=80, y=80)]*13, 1))

    def test_confirmed_death_remains_temporarily_dangerous(self):
        s = snapshot(enemies=False)
        s.recent_deaths = [{"frame": s.frame, "type": 25, "variant": 0, "instance_id": "dead1", "pos": {"x": 280, "y": 160}}]
        self.assertLess(ThreatField(s).gap((280, 160), (280, 160), 1, 10), 0)

    def test_swept_laser_endpoint_crossing(self):
        t = ThreatForecast("line", 100, 100, 0, 0, 1, segment_end=(400, 100))
        self.assertLess(t.gap((200, 90), (200, 110), 1, 0), 0)
