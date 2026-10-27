import json
from pathlib import Path
import tempfile
import unittest

from combat.damage import DamageCapture, extract_trace
from combat.damage_analysis import analyze_episode
from combat.runtime import RunLog
from tests.combat_fixtures import snapshot


class TestDamageCapture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def frame(self,capture,frame,hp=6,room="test:1",**changes):
        s=snapshot(frame=frame);s.health["red_hearts"]=hp;s.room_visit_id=room
        for key,value in changes.items():setattr(s,key,value)
        capture.snapshot(s,frame*10);return s

    def test_consecutive_losses_have_independent_windows(self):
        c=DamageCapture(self.root,authoritative=False,background=False)
        for f in range(1,80):self.frame(c,f,6 if f<20 else 5 if f<40 else 4)
        c.finish()
        self.assertEqual([r["end_frame"] for r in c.index],[20,40])
        for row in c.index:
            episode=json.loads(Path(row["artifact"]).read_text(encoding="utf-8"))
            self.assertEqual(episode["loss"]["damage"],1)
            self.assertEqual(episode["closure"],"post_window_collected")
            self.assertTrue(any(r["frame"]==row["end_frame"]+30 for r in episode["records"]))

    def test_attempt_without_health_loss_creates_no_episode(self):
        c=DamageCapture(self.root,authoritative=False,background=False)
        self.frame(c,1)
        c.consume({"kind":"game_message","sequence":12,"message":{"event":"PLAYER_DAMAGE_ATTEMPT","frame":1,"data":{"amount":10}}})
        self.frame(c,2);c.finish();self.assertEqual(c.index,[])

    def test_authorized_cost_and_container_change_do_not_create_damage(self):
        c=DamageCapture(self.root,background=False)
        s=self.frame(c,1);c.health(s,{"damage":0,"bones_lost":0},10)
        s=self.frame(c,2,4);c.health(s,{"damage":0,"bones_lost":0,"voluntary_spend":2},20)
        s=self.frame(c,3,2);c.health(s,{"damage":0,"bones_lost":0,"container_loss":2},30)
        c.finish();self.assertEqual(c.index,[])

    def test_healing_does_not_erase_previous_loss_and_gap_is_preserved(self):
        c=DamageCapture(self.root,authoritative=False,background=False)
        self.frame(c,1);self.frame(c,4,5);self.frame(c,5,6);c.finish()
        e=json.loads(Path(c.index[0]["artifact"]).read_text(encoding="utf-8"));a=analyze_episode(e)
        self.assertEqual(a["loss"]["damage"],1)
        self.assertIn("HEALTH_OBSERVATION_GAP",a["missing_evidence"])
        self.assertEqual(a["direct_source"]["kind"],"unknown")

    def test_room_change_and_shutdown_flush_partial_episode(self):
        for closure in ("context_changed","disconnected"):
            dest=self.root/closure;c=DamageCapture(dest,authoritative=False,background=False)
            self.frame(c,1);self.frame(c,2,5)
            if closure=="context_changed":self.frame(c,3,5,room="test:2")
            c.finish(closure)
            self.assertEqual(c.index[0]["closure"],closure)
            e=json.loads(Path(c.index[0]["artifact"]).read_text(encoding="utf-8"))
            self.assertTrue(all(r["room_visit_id"]=="test:1" for r in e["records"]))

    def test_source_identity_and_multiple_attempt_ambiguity(self):
        c=DamageCapture(self.root,authoritative=False,background=False)
        self.frame(c,1)
        event={"kind":"game_message","sequence":11,"message":{"event":"PLAYER_DAMAGE_ATTEMPT","frame":2,
            "data":{"source_type":9,"source_entity":{"instance_id":"p1"},"flags":0,"amount":100}}}
        c.consume(event);self.frame(c,2,5);c.finish()
        a=json.loads(Path(c.index[0]["artifact"]).with_suffix(".analysis.json").read_text())
        self.assertEqual(a["direct_source"]["entity_id"],"p1")
        self.assertEqual(a["loss"]["damage"],1)
        e=json.loads(Path(c.index[0]["artifact"]).read_text())
        attempt=next(r for r in e["records"] if r["kind"]=="damage_attempt")
        other={**attempt,"sequence":99,"data":{**attempt["data"],"source_type":10}}
        e["records"].append(other)
        self.assertEqual(analyze_episode(e)["direct_source"]["kind"],"unknown")

    def test_runlog_does_not_throttle_damage_triggers_and_bounds_by_frame(self):
        log=RunLog(self.root/"run")
        try:
            log.candidates(1,[]);log.candidates(100,[])
            self.assertEqual([r["frame"] for r in log.window],[100])
            for f in (120,154,190):log.trigger(f,["confirmed_damage"])
            log.trigger(191,["no_progress"]);log.trigger(192,["no_progress"])
        finally:log.damage.finish();log.file.close()
        rows=[json.loads(line) for line in (log.directory/"trace.jsonl").read_text().splitlines()]
        self.assertEqual(len([r for r in rows if r["kind"]=="debug_trigger"]),4)

    def test_live_authoritative_loss_and_replay_use_same_episode(self):
        log=RunLog(self.root/"run")
        for f,hp in ((1,6),(2,5),(3,6)):
            s=snapshot(frame=f);s.health["red_hearts"]=hp
            log("game_message",message={"payload":{"COMBAT_FRAME":s.model_dump()}})
            log.damage_observe(s,{"damage":int(f==2),"bones_lost":0,"voluntary_spend":0,"container_loss":0})
        log.damage.finish();log.file.close()
        result=extract_trace(log.directory/"trace.jsonl",self.root/"replay")
        self.assertEqual(result["ledger_mode"],"recorded_task_delta")
        self.assertEqual(result["episodes"][0]["episode_id"],log.damage.index[0]["episode_id"])

    def test_candidate_alternative_is_not_claimed_certainly_safe(self):
        c=DamageCapture(self.root,authoritative=False,background=False)
        self.frame(c,1)
        c.consume({"kind":"action_sent","sequence":11,"frame":1,"action_id":1,"sent":True,
            "move":[0,0],"shoot":[1,0],"metadata":{"diagnostics":{"decision_wall_ms":40}}})
        c.candidates(1,[{"move":[0,0],"shoot":[1,0],"risk":1,"safe":True,"clearance":1},
                        {"move":[1,0],"shoot":[1,0],"risk":0,"safe":True,"clearance":20}],12)
        self.frame(c,2,5);c.finish()
        a=json.loads(Path(c.index[0]["artifact"]).with_suffix(".analysis.json").read_text())
        comparison=next(f["value"] for f in a["facts"] if f["code"]=="recorded_candidate_comparison")
        self.assertEqual(comparison["counterfactual_status"],"old_model_prediction_not_proof_of_avoidability")
        self.assertTrue(comparison["lower_predicted_risk_alternatives"])
        self.assertIn("SELECTED_ACTION_APPLICATION_UNCONFIRMED",a["missing_evidence"])
