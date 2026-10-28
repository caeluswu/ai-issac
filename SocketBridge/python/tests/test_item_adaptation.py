import unittest
from combat.builds import BuildResolver
from combat.item_capabilities import resolve_items
from tests.combat_fixtures import snapshot
from tests.test_item_knowledge import pickup


class TestItemCapabilities(unittest.TestCase):
    def test_attributes_use_actual_panel_once(self):
        s=snapshot();s.inventory["collectibles"].update({"27":1,"32":1,"50":1,"4":1})
        s.stats.update(damage=8.25,max_fire_delay=4,speed=1.3)
        b=BuildResolver().update(s)
        self.assertEqual(b.damage_per_hit,8.25);self.assertEqual(b.fire_interval,5)
        self.assertEqual(b.family,"normal_tears")

    def test_movement_item_does_not_reset_attack_calibration(self):
        s=snapshot();r=BuildResolver();old=r.update(s)
        s.inventory["collectibles"]["27"]=1;s.stats["speed"]=1.3
        new=r.update(s)
        self.assertEqual(old.calibration_key,new.calibration_key)
        self.assertNotEqual(old.signature,new.signature)

    def test_mechanisms_require_actual_component_binding(self):
        from combat.models import WeaponObservation
        s=snapshot();s.inventory["collectibles"]["114"]=1
        with self.assertRaises(ValueError):resolve_items(s)
        with self.assertRaises(ValueError):resolve_items(s,knife=True)
        s.stats["weapon_type"]=-1;s.weapons=WeaponObservation(status="known",types=[4],unknowns=[])
        self.assertEqual(resolve_items(s,knife=True).family,"knife")
        s.inventory["collectibles"]["69"]=1
        with self.assertRaisesRegex(ValueError,"synergy"):resolve_items(s,knife=True,charge=True)

    def test_pickup_preview_does_not_mutate_build(self):
        s=snapshot();r=BuildResolver();old=r.update(s)
        self.assertTrue(r.supports_pickup(s,pickup(32)))
        self.assertTrue(r.supports_pickup(s,pickup(14)))
        self.assertTrue(r.supports_pickup(s,pickup(167)))
        self.assertTrue(r.supports_pickup(s,pickup(240)))
        self.assertTrue(r.supports_pickup(s,pickup(538)))
        for item_id in (8,17,75,98,108,154,169,213,313,581):
            self.assertTrue(r.supports_pickup(s,pickup(item_id)),item_id)
        self.assertFalse(r.supports_pickup(s,pickup(48)))
        self.assertEqual(r.update(s).signature,old.signature)
        self.assertNotIn("32",s.inventory["collectibles"])

    def test_treasure_attack_modifier_rebinds_without_changing_weapon_family(self):
        s=snapshot();r=BuildResolver();before=r.update(s)
        s.inventory["collectibles"]["154"]=1
        after=r.update(s)
        self.assertEqual(after.family,"normal_tears")
        self.assertIn("left_eye_damage",after.modifiers)
        self.assertNotEqual(before.calibration_key,after.calibration_key)
        s.inventory["collectibles"]["8"]=1
        familiar=r.update(s)
        self.assertEqual(after.calibration_key,familiar.calibration_key)

    def test_boss_stat_reward_rebinds_from_actual_panel_and_familiar_keeps_calibration(self):
        s=snapshot();r=BuildResolver();before=r.update(s)
        s.inventory["collectibles"]["14"]=1
        s.stats.update(speed=1.3,tear_range=320)
        after=r.update(s)
        self.assertNotEqual(before.calibration_key,after.calibration_key)
        self.assertGreater(after.move_speed,before.move_speed)
        self.assertGreater(after.effective_range,before.effective_range)
        s.inventory["collectibles"]["167"]=1
        familiar=r.update(s)
        self.assertEqual(after.calibration_key,familiar.calibration_key)
        self.assertEqual(familiar.family,"normal_tears")
        s.inventory["collectibles"]["254"]=1
        eye=r.update(s)
        self.assertIn("left_eye_damage",eye.modifiers)


class TestAdaptiveCharge(unittest.TestCase):
    def adapter(self,cache=None):
        from mechanisms.weapon import AdaptiveChargedBuild
        from mechanisms.program import Program
        from tests.test_mechanism_program import fixture
        from tests.test_mechanism_weapon import CALIBRATION
        return AdaptiveChargedBuild(Program(fixture()),CALIBRATION,cache)

    def charged(self):
        s=snapshot();s.inventory["collectibles"].update({"69":1,"4":1,"32":1,"27":1})
        s.stats.update(damage=7.,max_fire_delay=7,shot_speed=1.2,speed=1.3)
        return s

    def test_new_attributes_are_accepted_without_claiming_measured(self):
        a=self.adapter();s=self.charged();b=a.update(s)
        self.assertEqual(b.family,"charged_tears");self.assertEqual(b.damage_per_hit,7)
        self.assertEqual(b.calibration_state,"needs_probe");self.assertFalse(b.calibrated)
        self.assertTrue(a.supports_pickup(s,pickup(50)))
        self.assertFalse(a.supports_pickup(s,pickup(48)))

    def test_measurement_key_survives_movement_and_cached_load_is_not_new_sample(self):
        import tempfile
        from mechanisms.calibration import calibration_key
        with tempfile.TemporaryDirectory() as directory:
            a=self.adapter(directory);s=self.charged();a.update(s)
            value=dict(a.current_calibration);value.update(source="observed",observations=[{"tear_id":"measured","frame":5}])
            a.adopt(value);b=a.update(s)
            self.assertTrue(b.calibrated)
            old=calibration_key(s);s.stats["speed"]=1.7;s.stats["can_fly"]=True
            self.assertEqual(calibration_key(s),old)
            restored=self.adapter(directory).update(s)
            self.assertEqual(restored.calibration_source,"experience")
            self.assertEqual(restored.calibration_samples,1)
            s.inventory["collectibles"]["254"]=1
            self.assertNotEqual(calibration_key(s),old)
            s.stats["damage"]=8
            self.assertEqual(a.update(s).calibration_state,"needs_probe")

    def test_original_program_source_is_not_modified(self):
        from mechanisms.program import canonical
        a=self.adapter();before=canonical(a.program.document)
        a.update(self.charged())
        self.assertEqual(canonical(a.program.document),before)

    def test_known_room_damage_effect_does_not_authorize_active_pickup(self):
        from combat.models import EffectObservation
        s=snapshot()
        s.effects=EffectObservation.model_validate({"status":"known","unknowns":[],
            "items":[{"kind":"collectible","item_id":35,"count":1,"cooldown":0}]})
        self.assertEqual(resolve_items(s).family,"normal_tears")
        self.assertFalse(BuildResolver().supports_pickup(s,pickup(35)))
        s.effects.items[0].item_id=999
        with self.assertRaisesRegex(ValueError,"temporary effects unknown"):
            resolve_items(s)
