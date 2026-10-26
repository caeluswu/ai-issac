from dataclasses import replace
import unittest
from unittest.mock import patch
from combat.catalog import CATALOG, ComponentSpec, compile_plan, default_plan
from combat.component_contracts import AttackProposal, CapabilitySet, MotionProposal, ProposalContext, WaitEvidence
from combat.components import EXECUTABLES, DirectAim, Registry
from combat.controller import BuildResolver, JointActionSolver
from combat.models import BuildProfile, Vec
from tests.combat_fixtures import context, snapshot


class TestComponentBoundaries(unittest.TestCase):
    def solve_output(self, output, family="normal_tears", low_health=False, factory_error=False):
        class Fire:
            capabilities = CapabilitySet(family)
            def __init__(self, params):
                if factory_error: raise ValueError("factory fault")
            def propose(self, *args):
                if isinstance(output, Exception): raise output
                return output
        s = snapshot()
        if low_health: s.health["red_hearts"] = 2
        build = BuildProfile(family=family)
        raw = default_plan(context(s)).model_dump()
        raw["required_capabilities"] = [family]
        raw["profiles"]["safe"]["fire"] = {"component": "fire.boundary", "params": {}}
        raw["profiles"]["safe"]["aim"]["component"] = "aim.boundary"
        specs = {"fire.boundary": ComponentSpec("fire.boundary", {}, "fixture", (), weapons=(family,)),
                 "aim.boundary": ComponentSpec("aim.boundary", CATALOG["aim.direct"].parameters, "fixture", (), weapons=(family,))}
        with patch.dict(CATALOG, specs), patch.dict(EXECUTABLES.factories, {"fire.boundary": Fire, "aim.boundary": DirectAim}):
            solver = JointActionSolver()
            plan = compile_plan(raw, context(s), (family,))
            return solver.solve(s, plan, build), solver

    def test_invalid_attack_results_are_quarantined_before_selection(self):
        good = AttackProposal((1, 0), 1., 1., "fixture")
        invalid = [[], None, iter([good]), [good]*17, [{}],
                   [replace(good, shoot=(99, 0))], [replace(good, shoot=(True, 0))],
                   [replace(good, shoot=(1, 1))], [replace(good, hit_score=float("nan"))],
                   [replace(good, expected_damage=float("inf"))], [replace(good, hit_score=2)],
                   [replace(good, footprint=(0, 0, 0, 0, -1))],
                   [replace(good, footprint=(0, 0, 1e20, 0, 1))],
                   [replace(good, target_id="missing")],
                   [replace(good, wait=WaitEvidence("x", "x", 10, "expired"))],
                   [replace(good, wait=WaitEvidence("x", "x", 101, "excessive"))]]
        for output in invalid:
            with self.subTest(output=repr(output)[:100]):
                action, solver = self.solve_output(output)
                self.assertTrue(solver.modules["fire"].broken)
                self.assertTrue(action.diagnostics["component_failures"])
                self.assertIn(action.shoot, ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)))

    def test_other_family_fallback_never_becomes_normal_tears(self):
        for output, factory_error in (([], False), (RuntimeError("fault"), False), ([], True)):
            action, solver = self.solve_output(output, "charge_fixture", low_health=True, factory_error=factory_error)
            self.assertEqual(action.profile, "contract_emergency")
            self.assertEqual(action.shoot, (0, 0))
            self.assertEqual(action.diagnostics["fire_reason"], "no_compatible_fire_fallback")
            self.assertEqual(solver.modules["fire"].capabilities.family, "charge_fixture")
            self.assertEqual(solver.modules["fire"].fallback.capabilities.break_plain_poop, False)

    def test_other_family_emergency_keeps_compatible_fire(self):
        action, solver = self.solve_output([AttackProposal((0, -1), 1, 1, "charge_release")], "charge_fixture", low_health=True)
        self.assertEqual(action.shoot, (0, -1))
        self.assertFalse(solver.modules["fire"].broken)

    def test_stale_context_and_lease_are_rejected(self):
        ref = ProposalContext("test", "test:1", 1, 2, 10, 10)
        for changed in ({"run_id": "other"}, {"room_visit_id": "other"}, {"build_revision": 2},
                        {"catalog_revision": 3}, {"observed_frame": 9}, {"valid_until_frame": 9}, {"valid_until_frame": 100}):
            with self.subTest(changed=changed):
                _, solver = self.solve_output([AttackProposal((1, 0), 1, 1, "fixture", context=replace(ref, **changed))])
                self.assertTrue(solver.modules["fire"].broken)

    def test_explicit_current_context_and_wait_are_accepted(self):
        ref = ProposalContext("test", "test:1", 1, 2, 10, 11)
        _, solver = self.solve_output([AttackProposal((0, 0), 0, 0, "charge", WaitEvidence("a", "charge", 20, "known"), context=ref)])
        self.assertFalse(solver.modules["fire"].broken)

    def test_route_score_and_aim_outputs_are_validated(self):
        cases = [("target.threat_first", "score", float("nan")),
                 ("aim.predictive", "velocity", Vec(x=1e6, y=0)),
                 ("position.kite", "route", MotionProposal((Vec(x=0, y=0),)*257))]
        for identity, method, output in cases:
            registry = Registry(); registry.factories.update(EXECUTABLES.factories)
            original = registry.factories[identity]
            class Bad(original): pass
            setattr(Bad, method, lambda *args, value=output, **kwargs: value)
            registry.factories[identity] = Bad
            s = snapshot(); solver = JointActionSolver(registry)
            action = solver.solve(s, default_plan(context(s)), BuildResolver().update(s))
            self.assertTrue(action.diagnostics["component_failures"], identity)

    def test_scope_reset_and_same_update_hook_deduplication(self):
        class Observer:
            def __init__(self, params): self.updates = 0
            def begin_frame(self, *args): self.updates += 1
            def propose(self, *args): return [AttackProposal((0, 0), 0, 0, "fixture")]
        registry = Registry(); registry.factories.update(EXECUTABLES.factories)
        registry.factories["fire.normal_tears"] = Observer
        s = snapshot(); solver = JointActionSolver(registry); build = BuildProfile()
        plan = default_plan(context(s))
        solver.solve(s, plan, build); solver.solve(s, plan, build)
        old = solver.modules["fire"]
        self.assertEqual(old.updates, 1)
        s.room_visit_id = "test:2"
        solver.solve(s, default_plan(context(s)), build)
        self.assertIsNot(solver.modules["fire"], old)
        old = solver.modules["fire"]; build.revision = 2
        ref = context(s); ref.build_revision = 2
        solver.solve(s, default_plan(ref), build)
        self.assertIsNot(solver.modules["fire"], old)
        with self.assertRaisesRegex(ValueError, "STALE_PLAN"):
            solver.solve(s, plan, build)

    def test_radius_change_rebuilds_geometry(self):
        s = snapshot(); solver = JointActionSolver(); build = BuildResolver().update(s); plan = default_plan(context(s))
        solver.solve(s, plan, build); old = solver.geometry
        s.player.collision_radius += 4; s.frame += 1
        solver.solve(s, plan, build)
        self.assertIsNot(solver.geometry, old)
        self.assertEqual(solver.geometry.physical_radius, s.player.collision_radius)
