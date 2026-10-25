import json
from pathlib import Path
import unittest
import random
from unittest.mock import patch
from combat.catalog import default_plan, compile_plan
from combat.components import EXECUTABLES, Registry, AttackProposal, CapabilitySet
from combat.controller import JointActionSolver, BuildResolver
from combat.models import WorldSnapshot, Vec, BuildProfile
from combat.geometry import GeometryService
from combat.catalog import CATALOG, ComponentSpec
from combat.components import DirectAim
from combat.threats import EnemyAdapters, GenericEnemy, ThreatField, ThreatForecast
from tests.combat_fixtures import snapshot, context


class TestReplaceableComponents(unittest.TestCase):
    def test_enemy_attack_permission_is_replaceable_without_solver_edits(self):
        class ReflectionFixture(GenericEnemy):
            def attack_allowed(self, enemy, snapshot): return False
        s = snapshot(); adapters = EnemyAdapters(); adapters.register(10, 0, ReflectionFixture())
        a = JointActionSolver(enemy_adapters=adapters).solve(s, default_plan(context(s)), BuildResolver().update(s))
        self.assertEqual(a.shoot, (0, 0))
        self.assertEqual(a.diagnostics["fire_reason"], "attack_safety_withdrawal")
    def test_firing_opportunity_is_independent_of_primary_navigation_target(self):
        s = snapshot(); s.enemies[0].pos.y = 200
        other = s.enemies[0].model_copy(deep=True)
        other.instance_id = "e2"; other.pos = Vec(x=280, y=40)
        s.enemies.append(other)
        solver = JointActionSolver(); solver.target_id = "e1"; solver.target_since = s.frame
        a = solver.solve(s, default_plan(context(s)), BuildResolver().update(s))
        self.assertEqual(a.diagnostics["movement_target"], "e1")
        self.assertEqual(a.target, "e2")
        self.assertEqual(a.shoot, (0, -1))
        self.assertEqual(a.diagnostics["fire_reason"], "opportunistic_secondary")

    def test_entry_guard_has_no_offensive_objective(self):
        s = snapshot(); a = JointActionSolver().solve(s, default_plan(context(s)), BuildResolver().update(s), True)
        self.assertEqual(a.shoot, (0, 0)); self.assertIsNone(a.target)
        self.assertEqual(a.state, "ENTRY_GUARD")

    def test_contract_profile_preempts_low_health_and_is_explicit(self):
        s = snapshot(); s.health["red_hearts"] = 2
        solver = JointActionSolver()
        a = solver.solve(s, default_plan(context(s)), BuildResolver().update(s))
        self.assertEqual(a.profile, "contract_emergency")
        self.assertEqual(solver.modules["fire"].params["min_hit_score"], .1)
        self.assertNotEqual(a.shoot, (0, 0))

    def test_contract_profile_expires_when_condition_is_resolved(self):
        s = snapshot(); solver = JointActionSolver(); solver.emergency_until = s.frame+5
        plan, build = default_plan(context(s)), BuildResolver().update(s)
        self.assertEqual(solver.solve(s, plan, build).profile, "contract_emergency")
        s.frame += 6
        self.assertEqual(solver.solve(s, plan, build).profile, plan.default_profile)

    def test_planner_cannot_spoof_local_contract_profile(self):
        s = snapshot(); plan = default_plan(context(s)).model_dump()
        plan["profiles"]["contract_emergency"] = plan["profiles"].pop("safe")
        plan["default_profile"] = "contract_emergency"
        with self.assertRaisesRegex(ValueError, "reserved"): compile_plan(plan, context(s))

    def test_new_weapon_family_uses_adapter_and_catalog_without_scheduler_edits(self):
        class BuildFixture:
            def update(self, s): return BuildProfile(family="fixture_charge")
        class WeaponFixture:
            capabilities = CapabilitySet("fixture_charge", break_plain_poop=False)
            def __init__(self, params): pass
            def propose(self, *args): return [AttackProposal((0, 1), 1., 1., "fixture_release")]
            def firing_route(self, geometry, start, target, build, excluded=(), terrain=False):
                return geometry.path(start, Vec(x=200, y=200))
        s = snapshot(); s.stats["weapon_type"] = 99
        build = BuildResolver(adapters={99: BuildFixture()}).update(s)
        plan = default_plan(context(s)).model_dump(); plan["required_capabilities"] = ["fixture_charge"]
        plan["profiles"]["safe"]["fire"] = {"component": "fire.fixture_charge", "params": {}}
        plan["profiles"]["safe"]["aim"] = {"component": "aim.fixture", "params": {"max_lead_ticks": 12, "movement_inheritance": False}}
        specs = {"fire.fixture_charge": ComponentSpec("fire.fixture_charge", {}, "fixture", (), weapons=("fixture_charge",)),
                 "aim.fixture": ComponentSpec("aim.fixture", CATALOG["aim.direct"].parameters, "fixture", (), weapons=("fixture_charge",))}
        with patch.dict(CATALOG, specs), patch.dict(EXECUTABLES.factories, {"fire.fixture_charge": WeaponFixture, "aim.fixture": DirectAim}):
            compiled = compile_plan(plan, context(s), capabilities=("fixture_charge",))
            a = JointActionSolver().solve(s, compiled, build)
        self.assertEqual(a.shoot, (0, 1))
        self.assertEqual(a.diagnostics["route_goal"], [200, 200])

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
        plan["schema_version"] = 1; plan["context_ref"]["catalog_revision"] = 1; plan.pop("required_capabilities")
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
    def test_opportunistic_ray_cannot_bypass_another_enemy_death_hazard(self):
        s = snapshot(); ticking = s.enemies[0].model_copy(deep=True)
        ticking.type = 250; ticking.hp = 1; ticking.instance_id = "tick"
        ticking.pos = Vec(x=280, y=100); s.enemies.append(ticking)
        field = ThreatField(s); build = BuildResolver().update(s)
        self.assertFalse(field.attack_safe(s.enemies[0], build, [s.player.pos]*13, 1., (280, 160, 280, 40, 16)))
    def test_dense_backend_matches_all_threats_without_pruning(self):
        rng = random.Random(718)
        s = snapshot(enemies=False)
        s.projectiles["enemy_projectiles"] = [{"pos": {"x": rng.uniform(0, 640), "y": rng.uniform(0, 400)},
            "vel": {"x": rng.uniform(-12, 12), "y": rng.uniform(-12, 12)}, "collision_radius": rng.uniform(2, 9)} for _ in range(200)]
        s.bombs = [{"id": 1, "pos": {"x": 300, "y": 200}, "timer": 15, "explosion_radius": 80}]
        s.projectiles["lasers"] = [{"is_enemy": True, "pos": {"x": 100, "y": 100}, "angle": 30, "max_distance": 300, "collision_radius": 4}]
        field = ThreatField(s)
        self.assertTrue(field.dense)
        path = [Vec(x=250+i*2, y=200-i) for i in range(13)]
        batched = field.path_gaps(path, 14)
        for tick, value in enumerate(batched, 1):
            self.assertAlmostEqual(value, min(t.gap((path[tick-1].x, path[tick-1].y), (path[tick].x, path[tick].y), tick, 14) for t in field.threats), places=7)
        for tick in (1, 6, 12, 15, 22, 30, 45):
            a = (rng.uniform(100, 500), rng.uniform(100, 300)); b = (a[0]+5, a[1]-3)
            brute = min(t.gap(a, b, tick, 14) for t in field.threats)
            self.assertAlmostEqual(field.gap(a, b, tick, 14), brute, places=7)
            begin = max(0, tick-4)
            self.assertEqual(field.collides(a, b, tick, 14, begin), any(t.gap(a, b, tick, 14, begin) <= 0 for t in field.threats))
    def test_escape_is_checked_for_each_distinct_rollout_endpoint(self):
        endpoints = []
        original = ThreatField.escape
        def observe(field, geometry, position, *args):
            endpoints.append((position.x, position.y))
            return original(field, geometry, position, *args)
        s = snapshot()
        with patch.object(ThreatField, "escape", observe):
            JointActionSolver().solve(s, default_plan(context(s)), BuildResolver().update(s))
        self.assertEqual(len(set(endpoints)), 9)

    def test_ticking_spider_kill_is_unsafe_near_player(self):
        s = snapshot(); e = s.enemies[0]; e.type = 250; e.hp = 1
        self.assertFalse(ThreatField(s).attack_safe(e, BuildResolver().update(s), [e.pos]*13, 1))

    def test_airborne_hopper_retains_landing_threat(self):
        s = snapshot(); s.enemies[0].type = 29; s.enemies[0].collision_class = 0
        self.assertTrue(ThreatField(s).threats)
    def test_escape_reserve_follows_withdrawal_instead_of_holding_still(self):
        s = snapshot(enemies=False)
        s.projectiles["enemy_projectiles"] = [{"pos": {"x": 200, "y": 160}, "vel": {"x": 4.2, "y": 0}, "collision_radius": 5}]
        fraction, slack, directions = ThreatField(s).escape(GeometryService(s), s.player.pos, 4.2, 10)
        self.assertGreaterEqual(directions, 1)
        self.assertGreaterEqual(slack, 6)
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
