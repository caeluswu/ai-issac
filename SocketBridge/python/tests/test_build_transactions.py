import unittest
from combat.builds import BuildResolver, observed_build
from combat.catalog import default_plan
from combat.controller import JointActionSolver
from combat.ledgers import HealthLedger, ResourceLedger
from combat.observation import CombatJudge
from tests.combat_fixtures import snapshot, context


class TestBuildTransactions(unittest.TestCase):
    def test_supported_pickup_recompiles_without_double_adding_stats(self):
        s = snapshot(); resolver = BuildResolver(); old = resolver.update(s)
        s.inventory["collectibles"]["4"] = 1; s.stats["damage"] = 4.; s.frame += 1
        new = resolver.update(s)
        self.assertGreater(new.revision, old.revision)
        self.assertEqual(new.damage_per_hit, 4.)
        self.assertIn("inventory", new.changed_domains)
        self.assertEqual(resolver.update(s).revision, new.revision)
        with self.assertRaisesRegex(ValueError, "STALE_PLAN"):
            JointActionSolver().solve(s, default_plan(context(s)), new)
        ref = context(s); ref.build_revision = new.revision
        self.assertIsNotNone(JointActionSolver().solve(s, default_plan(ref), new))

    def test_flight_and_size_invalidate_only_relevant_calibration(self):
        s = snapshot(); resolver = BuildResolver(); old = resolver.update(s)
        s.stats["can_fly"] = True; s.player.collision_radius = 14; s.frame += 1
        new = resolver.update(s)
        self.assertEqual(new.changed_domains, ["movement"])
        self.assertEqual(old.calibration_key, new.calibration_key)
        self.assertNotEqual(old.signature, new.signature)

    def test_new_build_does_not_reuse_in_flight_old_tears(self):
        s = snapshot(); resolver = BuildResolver(); resolver.update(s)
        for index in range(3):
            s.frame += 1
            s.projectiles["player_tears"] = [{"id": index, "pos": s.player.pos.model_dump(), "vel": {"x": 9, "y": 0}}]
            build = resolver.update(s)
        self.assertTrue(build.calibrated)
        s.stats["shot_speed"] = 2; s.frame += 1
        new = resolver.update(s)
        self.assertEqual(new.calibration_samples, 0)
        self.assertEqual(new.tear_speed, 20)

    def test_legacy_invalid_slot_is_migrated_without_erasing_evidence(self):
        s = snapshot(); s.inventory["collectibles"]["733"] = 50331651
        build = BuildResolver().update(s)
        self.assertNotIn("733", observed_build(s)["inventory"]["collectibles"])
        self.assertIn("733", s.inventory["collectibles"])
        self.assertEqual(build.revision, 1)

    def test_nonfinite_observed_stats_and_unknown_item_do_not_enter_solver(self):
        for value in (float("nan"), float("inf")):
            s = snapshot(); s.stats["speed"] = value
            with self.assertRaises(ValueError): BuildResolver().update(s)
        s = snapshot(); s.inventory["collectibles"]["149"] = 1
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED_BUILD"): BuildResolver().update(s)


class TestHealthAndResources(unittest.TestCase):
    def test_empty_bone_container_is_alive(self):
        s = snapshot(); s.health.update(red_hearts=0, soul_hearts=0, bone_hearts=1)
        self.assertNotEqual(CombatJudge().observe(s), "DEAD")

    def test_revive_has_bounded_wait_and_can_return_to_alive(self):
        ledger = HealthLedger(); s = snapshot(); s.health["extra_lives"] = 1; ledger.observe(s)
        s = s.model_copy(deep=True); s.frame += 1; s.player.dead = True; s.health["red_hearts"] = 0
        self.assertEqual(ledger.observe(s).state, "reviving")
        revived = s.model_copy(deep=True); revived.frame += 90; revived.player.dead = False; revived.health.update(red_hearts=2, extra_lives=0)
        self.assertEqual(ledger.observe(revived).state, "alive")
        self.assertEqual(ledger.damage, 6)
        failed = HealthLedger(); failed.observe(s); s.frame += 121
        self.assertEqual(failed.observe(s).state, "dead")

    def test_damage_healing_container_change_and_authorized_spend_are_distinct(self):
        ledger = HealthLedger(); s = snapshot(); s.health["max_hearts"] = 6; ledger.observe(s)
        hurt = s.model_copy(deep=True); hurt.frame += 1; hurt.health["red_hearts"] = 4
        self.assertEqual(ledger.observe(hurt).damage, 2)
        healed = s.model_copy(deep=True); healed.frame += 2; ledger.observe(healed)
        changed = healed.model_copy(deep=True); changed.frame += 1; changed.health.update(max_hearts=4, red_hearts=4)
        self.assertEqual(ledger.observe(changed).container_loss, 2)
        ledger.authorize("deal", 2, changed.frame+10)
        paid = changed.model_copy(deep=True); paid.frame += 1; paid.health["red_hearts"] = 2
        self.assertEqual(ledger.observe(paid).voluntary_spend, 2)
        ledger.observe(paid)
        self.assertEqual(ledger.damage, 2)
        self.assertEqual(ledger.voluntary_spend, 2)

    def test_damage_attempt_cannot_be_reclassified_as_container_loss(self):
        ledger = HealthLedger(); s = snapshot(); s.health["max_hearts"] = 6; ledger.observe(s)
        ledger.damage_attempt(s.frame)
        s = s.model_copy(deep=True); s.frame += 1; s.health.update(max_hearts=4, red_hearts=4)
        self.assertEqual(ledger.observe(s).damage, 2)

    def test_resource_gains_never_cancel_prior_spending(self):
        ledger = ResourceLedger(); s = snapshot(); s.inventory.update(coins=5, keys=2, bombs=1); ledger.observe(s)
        s.frame += 1; s.inventory["coins"] = 2; ledger.observe(s)
        s.frame += 1; s.inventory["coins"] = 10; ledger.observe(s)
        self.assertEqual(ledger.spent["coins"], 3)
