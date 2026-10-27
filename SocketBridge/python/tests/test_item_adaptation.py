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
        s=snapshot();s.inventory["collectibles"]["48"]=1
        with self.assertRaises(ValueError):resolve_items(s)
        self.assertEqual(resolve_items(s,piercing=True).modifiers,("piercing",))
        s.inventory["collectibles"]["69"]=1
        self.assertEqual(resolve_items(s,piercing=True,charge=True).family,"charged_tears")

    def test_pickup_preview_does_not_mutate_build(self):
        s=snapshot();r=BuildResolver();old=r.update(s)
        self.assertTrue(r.supports_pickup(s,pickup(32)))
        self.assertFalse(r.supports_pickup(s,pickup(48)))
        self.assertEqual(r.update(s).signature,old.signature)
        self.assertNotIn("32",s.inventory["collectibles"])
