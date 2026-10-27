"""Host integration fixtures are explicitly engineering-authored, not skills."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from combat.catalog import CATALOG,FAMILY_DEFAULTS,default_plan
from combat.components import EXECUTABLES
from combat.builds import BuildResolver
from combat.controller import JointActionSolver
from combat.geometry import GeometryService
from mechanisms.program import Program
from mechanisms.weapon import ChargedBuild,ProgramWeapon,register_program,FAMILY
from tests.combat_fixtures import snapshot,context
from tests.test_mechanism_program import fixture,feedback


CALIBRATION=json.loads((Path(__file__).resolve().parents[3]/"configs/mechanisms/chocolate-milk-j835.json").read_text())


def charged_snapshot():
    s=snapshot();s.room_visit_id="test:room1";s.frame=100
    s.stats.update(damage=3.5,max_fire_delay=10,shot_speed=1.,player_type=0,weapon_type=1)
    s.inventory["collectibles"]={"69":1,"105":1}
    s.enemies[0].pos.x=280;s.enemies[0].pos.y=40
    s.weapon_feedback={"schema_version":1,"provenance":"engine_input_callback",
                       "history":[feedback(68)]+[feedback(f,(0,-1)) for f in range(69,100)]}
    return s


class TestMechanismWeapon(unittest.TestCase):
    def test_default_resolver_still_rejects_69(self):
        with self.assertRaisesRegex(ValueError,"UNSUPPORTED_BUILD"):BuildResolver().update(charged_snapshot())

    def test_adapter_checks_exact_calibration_and_no_synergies(self):
        p=Program(fixture());adapter=ChargedBuild(p,CALIBRATION)
        s=charged_snapshot();self.assertEqual(adapter.update(s).family,FAMILY)
        s.stats["damage"]=4
        with self.assertRaises(ValueError):adapter.update(s)
        s.stats["damage"]=3.5;s.inventory["collectibles"]["1"]=1
        with self.assertRaises(ValueError):adapter.update(s)

    def test_family_transition_increments_revision(self):
        s=charged_snapshot();s.inventory["collectibles"].pop("69")
        r=BuildResolver(mechanism_adapter=ChargedBuild(Program(fixture()),CALIBRATION))
        before=r.update(s);s.inventory["collectibles"]["69"]=1;after=r.update(s)
        self.assertGreater(after.revision,before.revision)
        self.assertNotEqual(after.signature,before.signature)
        self.assertEqual(r.update(s).revision,after.revision)

    def test_unsafe_release_withdrawal_keeps_observed_hold(self):
        d=fixture();d["decide"]={"mode":2,"shoot_x":0,"shoot_y":0}
        s=charged_snapshot();p=Program(d);b=ChargedBuild(p,CALIBRATION).update(s)
        with patch.dict(CATALOG),patch.dict(EXECUTABLES.factories),patch.dict(FAMILY_DEFAULTS):
            register_program(p,CALIBRATION)
            plan=default_plan(context(s),family=FAMILY)
            weapon=ProgramWeapon({},p,CALIBRATION);weapon.begin_frame(s,b)
            proposals=weapon.propose(s,b,plan.profiles["safe"],s.hostiles[0],s.player.vel,GeometryService(s))
            self.assertEqual(len(proposals),1)
            self.assertEqual(proposals[0].shoot,(0,0))
            self.assertEqual(proposals[0].withdrawal_shoot,(0,-1))
            self.assertTrue(proposals[0].footprint)
            self.assertGreater(proposals[0].hit_score,0)

    def test_idle_is_also_checked_as_release(self):
        s=charged_snapshot();p=Program(fixture());b=ChargedBuild(p,CALIBRATION).update(s)
        with patch.dict(CATALOG),patch.dict(EXECUTABLES.factories),patch.dict(FAMILY_DEFAULTS):
            register_program(p,CALIBRATION);plan=default_plan(context(s),family=FAMILY)
            w=ProgramWeapon({},p,CALIBRATION);w.begin_frame(s,b)
            a=w.propose(s,b,plan.profiles["safe"],s.hostiles[0],s.player.vel,GeometryService(s))[0]
            self.assertEqual(a.reason,"program_release");self.assertTrue(a.footprint)

    def test_solver_withdraws_release_by_keeping_hold_near_death_explosion(self):
        d=fixture();d["decide"]={"mode":2,"shoot_x":0,"shoot_y":0}
        s=charged_snapshot();s.enemies[0].type=250;s.enemies[0].hp=1;s.enemies[0].pos.y=140
        p=Program(d);b=ChargedBuild(p,CALIBRATION).update(s)
        with patch.dict(CATALOG),patch.dict(EXECUTABLES.factories),patch.dict(FAMILY_DEFAULTS):
            register_program(p,CALIBRATION)
            action=JointActionSolver().solve(s,default_plan(context(s),family=FAMILY),b)
            self.assertEqual(action.diagnostics["fire_reason"],"attack_safety_withdrawal")
            self.assertEqual(action.shoot,(0,-1))

    def test_new_family_uses_published_default_without_scheduler_algorithm(self):
        d=fixture();d["decide"]={"mode":1,"shoot_x":["input","shoot_x"],"shoot_y":["input","shoot_y"]}
        s=charged_snapshot();p=Program(d);r=BuildResolver(mechanism_adapter=ChargedBuild(p,CALIBRATION));b=r.update(s)
        with patch.dict(CATALOG),patch.dict(EXECUTABLES.factories),patch.dict(FAMILY_DEFAULTS):
            fire=register_program(p,CALIBRATION)
            ref=context(s).model_copy(update={"build_revision":b.revision})
            plan=default_plan(ref,family=FAMILY)
            action=JointActionSolver().solve(s,plan,b)
            self.assertNotEqual(action.shoot,(0,0))
            self.assertEqual(plan.profiles["safe"].fire.component,fire)
            self.assertFalse(action.diagnostics["component_failures"])
