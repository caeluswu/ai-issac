"""Interface fixtures are test doubles, never published model artifacts."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from combat.builds import BuildResolver,digest
from combat.catalog import CATALOG,FAMILY_DEFAULTS
from combat.components import EXECUTABLES
from combat.models import WeaponObservation
from combat.item_capabilities import attack_identity
from mechanisms.knife import KnifeBuild,KnifeWeapon,register_knife
from mechanisms.javascript import AttackModelRuntime,JSWorker
from tests.combat_fixtures import snapshot
from tests.test_item_knowledge import pickup


FIXTURE="""function init(c){return {calls:0};}
function observe(c,f,s){s.calls++;return s;}
function proposeBatch(c,cs,s){return cs.map(x=>({candidate_id:x.id,
shoot:c.frame%20<10?[1,0]:[0,0],mode:c.frame%20<10?'hold':'release',
hit_score:.5,expected_damage:1,target_ids:[]}));} """


def knife_snapshot(frame=10):
    s=snapshot(frame);s.inventory['collectibles']['114']=1;s.stats['weapon_type']=-1
    s.weapons=WeaponObservation(status='known',types=[4],unknowns=[])
    return s


def context(frame=1,room='r'):
    return dict(run_id='test',room_visit_id=room,build_signature='b',snapshot_id=f'r:{room}:{frame}',frame=frame,enemies=[])


class KnifeAdapterTests(unittest.TestCase):
    def test_known_weapon_transition_and_pickup_preview(self):
        r=BuildResolver();r.adapters[-1]=KnifeBuild();r.knife_enabled=True
        old=r.update(snapshot());self.assertTrue(r.supports_pickup(snapshot(),pickup(114)))
        b=r.update(knife_snapshot());self.assertEqual(b.family,'knife');self.assertGreater(b.revision,old.revision)
        unknown=knife_snapshot();unknown.weapons.types=[2]
        with self.assertRaises(ValueError):r.update(unknown)

    def test_calibration_separate_from_program_and_attack_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            a=KnifeBuild(directory);s=knife_snapshot();b=a.update(s)
            self.assertEqual(b.calibration_state,'needs_probe')
            value=dict(scope_key=digest(['knife',attack_identity(s)]),calibration_id='k',cycles=[{}, {}, {}],max_observed_distance=180,observed_speed=11)
            a.adopt(value);b=a.update(s);self.assertTrue(b.calibrated)
            s.inventory['collectibles']['27']=1;s.stats['speed']=1.3
            self.assertTrue(a.update(s).calibrated)
            self.assertEqual(KnifeBuild(directory).update(s).calibration_source,'experience')
            s.stats['damage']=7;self.assertEqual(a.update(s).calibration_state,'needs_probe')

    def test_release_footprint_and_safe_withdrawal_preserve_hold(self):
        from combat.catalog import default_plan
        from tests.combat_fixtures import context as ref
        with patch.dict(CATALOG),patch.dict(EXECUTABLES.factories),patch.dict(FAMILY_DEFAULTS):
            register_knife();s=knife_snapshot();b=KnifeBuild().update(s)
            w=KnifeWeapon({});w.attack_candidate_id=0
            w.attack_model_results={(0,(0,0)):{'shoot':[0,0],'mode':'release','hit_score':.5,'expected_damage':4,'target_ids':['e1'],'position_score':2}}
            s.weapon_feedback={'history':[{'consistent':True,'values':{'right':True}}]}
            plan=default_plan(ref(s),family='knife')
            p=w.propose(s,b,plan.profiles[plan.default_profile],None,s.player.vel,None)[0]
            self.assertGreater(p.footprint[2],s.player.pos.x);self.assertEqual(p.withdrawal_shoot,(1,0))
            self.assertEqual(p.position_bias,2)


class JavaScriptKnifeTests(unittest.TestCase):
    def test_persistent_context_once_per_snapshot_and_scope_reset(self):
        r=AttackModelRuntime(FIXTURE)
        try:
            candidates=[{'id':0}]
            r.evaluate(context(),candidates);r.evaluate(context(),candidates)
            self.assertEqual(r.calls,1);self.assertEqual(r.state['calls'],1)
            r.evaluate(context(2),candidates);self.assertEqual(r.state['calls'],2)
            r.evaluate(context(3,'other'),candidates);self.assertEqual(r.state['calls'],1)
            r.evaluate(context(15),candidates);self.assertEqual(r.result[(0,(0,0))]['mode'],'release')
        finally:r.close()

    def test_invalid_mode_and_unknown_target_fail_without_host_exception(self):
        for source in (FIXTURE.replace("mode:c.frame%20<10?'hold':'release'","mode:'idle'"),FIXTURE.replace('target_ids:[]','target_ids:["invented"]')):
            r=AttackModelRuntime(source)
            try:
                self.assertEqual(r.evaluate(context(),[{'id':0}]),{})
                self.assertIsNotNone(r.failure);self.assertTrue(r.worker.closed)
            finally:r.close()

    def test_cpu_loop_is_bounded_and_no_host_io(self):
        r=AttackModelRuntime(FIXTURE.replace('s.calls++;return s;','while(true){}'))
        try:
            self.assertEqual(r.evaluate(context(),[{'id':0}]),{});self.assertIsNotNone(r.failure)
        finally:r.close()
        w=JSWorker("function init(){return null} function observe(c,f,s){return s} function proposeBatch(){return [{apis:typeof fetch+','+typeof require+','+typeof Python}]} ")
        try:
            result=w.call({'type':'tick','payload':{'context':{},'feedback':{},'reset':True,'candidates':[]}})
            self.assertEqual(result['proposals'][0]['apis'],'undefined,undefined,undefined')
        finally:w.close()

    def test_sdk_check_is_not_combat_proof(self):
        from mechanisms.knife_developer import check_source
        value=check_source(FIXTURE)
        self.assertTrue(value['passed']);self.assertEqual(value['scope'],'SDK_only_not_combat_validation')

    def test_model_patch_package_checks_raw_patch_and_parent(self):
        from mechanisms.knife_developer import save_model_patch,AttackStore
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            parent_manifest={'schema_version':1,'language':'javascript','component_id':'weapon.moms_knife',
                'author':'deepseek','response_id':'parent','source_sha256':hashlib.sha256(FIXTURE.encode()).hexdigest()}
            replacement=FIXTURE[FIXTURE.index('function proposeBatch'):].strip()+"\n"
            replacement=replacement.replace('hit_score:.5','hit_score:.6')
            raw=json.dumps({'function':'proposeBatch','replacement':replacement,'summary':'test'})
            value=save_model_patch((FIXTURE,parent_manifest,Path(directory)),raw,{'response_id':'patch','response_model':'fixture'},directory)
            self.assertEqual(value[1]['author'],'deepseek_patch')
            self.assertEqual(AttackStore(directory).load(value[2])[0],value[0])
            (value[2]/'parent-source.js').write_text('changed',encoding='utf-8')
            with self.assertRaises(ValueError):AttackStore(directory).load(value[2])

    def test_common_batch_invoked_once_and_carries_knife_observation(self):
        from mechanisms.attack_batch import prepare_batch
        from combat.geometry import GeometryService
        from combat.components import PredictiveAim
        from types import SimpleNamespace
        class Runtime:
            calls=0;calibration_summary={'completed_cycles':3,'charge_per_held_update_estimate':.025}
            def evaluate(self,c,cs,feedback=None):
                self.calls+=1;self.context=c;self.candidates=cs;return {}
        r=Runtime();s=knife_snapshot();s.projectiles['player_knives']=[{'instance_id':'blade','is_flying':False,'charge':0}]
        s.weapon_feedback={'history':[{'frame':f,'consistent':True,'queried':{k:True for k in ('left','right','up','down')},
            'values':{'right':True}} for f in range(s.frame-4,s.frame)]}
        prepare_batch(r,s,KnifeBuild().update(s),None,GeometryService(s),((0,0),(1,0)),KnifeWeapon({}),PredictiveAim({'max_lead_ticks':12,'movement_inheritance':True}))
        self.assertEqual(r.calls,1);self.assertEqual(len(r.candidates),2)
        self.assertEqual(r.context['knives'][0]['instance_id'],'blade')
        self.assertEqual(r.context['calibration']['completed_cycles'],3)
        self.assertEqual(r.context['knives'][0]['held_updates_observed'],4)
        self.assertAlmostEqual(r.context['knives'][0]['charge'],.1)
        self.assertEqual(r.context['knives'][0]['engine_charge'],0)
        self.assertEqual(s.projectiles['player_knives'][0]['charge'],0)

    def test_actual_release_charge_calibrates_input_estimate(self):
        from mechanisms.knife_calibration import charge_rate
        cycles=[{'hold_updates':n,'observations':[{'knife':{'is_flying':True,'charge':n/40}}]} for n in (3,10,20)]
        self.assertAlmostEqual(charge_rate({'cycles':cycles}),.025)


class ItemBudgetTests(unittest.TestCase):
    def test_old_attribution_rows_preserved_and_gap_quota_enforced(self):
        from combat.exploration_budget import ExplorationBudget
        from campaign.budget import BudgetExceeded
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'budget.sqlite';old=ExplorationBudget(path)
            for _ in range(3):old.reserve([])
            new=ExplorationBudget(path,phase='item_adaptation',gap='moms_knife')
            for _ in range(3):new.reserve([])
            self.assertEqual(new.report()['requests'],6)
            with self.assertRaises(BudgetExceeded):new.reserve([])
            with self.assertRaises(BudgetExceeded):ExplorationBudget(path).reserve([])
            new.grant_gap('user:approved-one-extra-knife-request')
            self.assertEqual(new.report()['gap_request_limit'],4)
            self.assertEqual(new.reserve([])['attempt_id'],7)
            with self.assertRaises(ValueError):new.grant_gap('user:approved-one-extra-knife-request')
            with self.assertRaises(BudgetExceeded):new.reserve([])
