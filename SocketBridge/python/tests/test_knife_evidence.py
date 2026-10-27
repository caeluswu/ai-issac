import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from mechanisms.knife_evidence import summarize


class KnifeEvidenceTests(unittest.TestCase):
    def test_callback_alone_does_not_count_and_later_hp_drop_does(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'trace.jsonl'
            def frame(n,flying,hp):
                return {'kind':'game_message','message':{'payload':{'COMBAT_FRAME':{
                    'run_id':'r','room_visit_id':'r:1','frame':n,'player':{'pos':{'x':0,'y':0},'collision_radius':10},
                    'projectiles':{'player_knives':[{'instance_id':'k','is_flying':flying}]},
                    'enemies':[{'instance_id':'e','pos':{'x':100,'y':0},'collision_radius':10,'hp':hp}],
                    'recent_deaths':[]}}}}
            event={'kind':'game_message','message':{'event':'KNIFE_DAMAGE_ATTEMPT','data':{
                'run_id':'r','room_visit_id':'r:1','callback_frame':2,'source_knife_id':'k','target_id':'e'}}}
            lines=[frame(0,False,10),frame(1,True,10),event,frame(2,True,10),frame(3,True,10),frame(4,False,10)]
            path.write_text('\n'.join(json.dumps(x) for x in lines)+'\n',encoding='utf-8')
            report=SimpleNamespace(result='CLEARED',accepted_actions=1,damage_half_hearts=0,combat_ticks=10,wall_seconds=1)
            proof=summarize(path,'hash',report,{'completed_cycles':3})
            self.assertEqual(proof['confirmed_thrown_hits'],0);self.assertFalse(proof['passed'])
            lines[-2]=frame(3,True,7)
            lines.append({'kind':'action_sent','action_id':1,'metadata':{'diagnostics':{'fire_reason':'model_knife_hold'}}})
            path.write_text('\n'.join(json.dumps(x) for x in lines)+'\n',encoding='utf-8')
            proof=summarize(path,'hash',report,{'completed_cycles':3})
            self.assertEqual(proof['confirmed_thrown_hits'],1);self.assertTrue(proof['passed'])
