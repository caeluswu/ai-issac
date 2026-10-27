import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from facade import SocketBridge
from combat.agent_store import AgentStore
from combat.builds import BuildResolver
from combat.catalog import default_plan
from combat.controller import JointActionSolver
from combat.item_knowledge import KnowledgeGateway
from combat.item_learning import ItemLearning
from combat.runtime import CombatSession, RunLog
from tests.combat_fixtures import snapshot, context
from tests.test_combat_session import SimulatedRoom
from tests.test_item_knowledge import package_fixture, pickup


class TestExperienceStore(unittest.TestCase):
    def test_claims_require_matching_evidence_and_conflicts_survive_reopen(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/"state.sqlite"; store=AgentStore(path)
            ref=store.measurement("sig","launch_speed",{"value":10.,"source_mode":"normal_play"})
            with self.assertRaises(ValueError): store.accept_claim("other","launch_speed",10.,[ref])
            with self.assertRaises(ValueError): store.accept_claim("sig","launch_speed",100.,[ref])
            claim=store.accept_claim("sig","launch_speed",10.,[ref])
            self.assertEqual(store.retrieve("sig")["experience_revision"],1)
            store.accept_claim("sig","launch_speed",10.,[ref])
            self.assertEqual(store.retrieve("sig")["experience_revision"],1)
            store.contradict(claim,{"reason":"fixture_counterexample"})
            reopened=AgentStore(path).retrieve("sig")
            self.assertEqual(reopened["local_claims"],[])
            self.assertTrue(reopened["counterexamples"])
            self.assertEqual(reopened["counterexamples"][0]["source_mode"],"normal_play")
            self.assertEqual(reopened["experience_revision"],2)


class TestItemLearning(unittest.IsolatedAsyncioTestCase):
    async def test_owned_stat_item_with_health_change_is_recorded_not_lost(self):
        class StatRoom(SimulatedRoom):
            async def data(self):
                if self.s.pickups and not self.s.pickups[0].is_empty and self.s.player.pos.distance(self.s.pickups[0].pos)<18:
                    self.s.pickups[0].is_empty=True
                    self.s.pickups[0].sub_type=None
                    self.s.pickups[0].identity_visibility="unknown"
                    self.s.inventory["collectibles"]["14"]=1
                    self.s.stats.update(speed=1.3,tear_range=320)
                    self.s.health["red_hearts"]=5
                await super().data()
        with tempfile.TemporaryDirectory() as root:
            learning=ItemLearning(KnowledgeGateway(package_fixture(root),AgentStore(Path(root)/"agent.sqlite")),source_mode="simulated_game")
            game=StatRoom();game.s.pickups=[pickup(14)]
            bridge=SocketBridge(port=0);log=RunLog(Path(root)/"run")
            session=CombatSession(bridge,None,game,log,ready_timeout=3,learning=learning)
            async def boot(_):await game.connect(bridge._server._server.sockets[0].getsockname()[1])
            try:
                s=await session.open(bootstrap=boot);build=session.build_resolver.update(s)
                await learning.prepare_room(s,build)
                target=s.pickups[0];decision=learning.decisions[(target.instance_id,target.sub_type,target.price)]
                result=await asyncio.wait_for(learning.pickups.execute(session,s,build,target,decision),8)
                self.assertEqual(result["result"],"confirmed",result)
                self.assertEqual(result["health_before"]["red_hearts"],6)
                self.assertEqual(result["health_after"]["red_hearts"],5)
                self.assertEqual(result["inventory_after"]["collectibles"]["14"],1)
            finally:
                await session.close();await game.close();log.file.close()

    async def test_pickup_transaction_moves_normally_and_confirms_inventory(self):
        class PickupRoom(SimulatedRoom):
            touched_frame = None
            async def data(self):
                if self.s.pickups and self.s.player.pos.distance(self.s.pickups[0].pos)<18 and self.touched_frame is None:
                    self.touched_frame=self.s.frame
                    self.s.pickups[0].is_empty=True;self.s.pickups[0].sub_type=None;self.s.pickups[0].identity_visibility="unknown"
                    self.s.inventory["queued_collectible_id"]=1
                if self.touched_frame is not None and self.s.frame-self.touched_frame>=40:
                    self.s.inventory["collectibles"]["1"]=1;self.s.stats["max_fire_delay"]=7
                    self.s.inventory["queued_collectible_id"]=0
                await super().data()
        with tempfile.TemporaryDirectory() as root:
            learning=ItemLearning(KnowledgeGateway(package_fixture(root),AgentStore(Path(root)/"agent.sqlite")),source_mode="simulated_game")
            game=PickupRoom();game.s.pickups=[pickup()]
            bridge=SocketBridge(port=0);log=RunLog(Path(root)/"run")
            session=CombatSession(bridge,None,game,log,ready_timeout=3,learning=learning)
            async def boot(_): await game.connect(bridge._server._server.sockets[0].getsockname()[1])
            try:
                s=await session.open(bootstrap=boot);build=session.build_resolver.update(s)
                await learning.prepare_room(s,build)
                target=s.pickups[0];decision=learning.decisions[(target.instance_id,target.sub_type,target.price)]
                result=await asyncio.wait_for(learning.pickups.execute(session,s,build,target,decision),8)
                self.assertEqual(result["result"],"confirmed",result)
                self.assertGreater(result["build_revision"],build.revision)
                self.assertEqual(learning.gateway.store.get("pickup",result["intent_id"])["result"],"confirmed")
                self.assertTrue(game.inputs)
                self.assertTrue(all(not x["use_item"] and not x["use_bomb"] and x["shoot"]=={"x":0,"y":0} for x in game.inputs))
                before=len(game.inputs)
                duplicate=await learning.pickups.execute(session,s,build,target,decision)
                self.assertEqual(duplicate,result)
                self.assertEqual(len(game.inputs),before)
            finally:
                await session.close();await game.close();log.file.close()

    async def test_passive_combat_memory_reopens_and_never_launders_cached_samples(self):
        class TearRoom(SimulatedRoom):
            async def data(self):
                if self.shoot!=(0,0):
                    self.s.projectiles["player_tears"]=[{"id":self.s.frame,"owner_type":1,"pos":self.s.player.pos.model_dump(),"vel":{"x":10,"y":0}}]
                await super().data()
        class LocalPlanner:
            async def plan(self,s,build,revision=0,feedback=None):
                ref=context(s);ref.build_revision=build.revision;ref.experience_revision=build.experience_revision;ref.plan_revision=revision
                return default_plan(ref)
        with tempfile.TemporaryDirectory() as root:
            package=package_fixture(root);path=Path(root)/"agent.sqlite"
            learning=ItemLearning(KnowledgeGateway(package,AgentStore(path)),source_mode="simulated_game")
            game=TearRoom(hit_threshold=12);bridge=SocketBridge(port=0);log=RunLog(Path(root)/"run")
            session=CombatSession(bridge,LocalPlanner(),game,log,ready_timeout=3,learning=learning)
            async def boot(_): await game.connect(bridge._server._server.sockets[0].getsockname()[1])
            try:
                result=await asyncio.wait_for(session.run(bootstrap=boot),15)
                self.assertTrue(result.success,result.model_dump())
            finally: await game.close()
            fresh=snapshot();resolver=BuildResolver();build=resolver.update(fresh)
            next_learning=ItemLearning(KnowledgeGateway(package,AgentStore(path)),source_mode="simulated_game")
            restored=await next_learning.apply(fresh,resolver,build)
            self.assertEqual(restored.calibration_source,"experience")
            self.assertEqual(restored.calibration_samples,0)
            self.assertEqual(restored.tear_speed,10.)
            with self.assertRaisesRegex(ValueError,"STALE_EXPERIENCE"):
                JointActionSolver().solve(fresh,default_plan(context(fresh)),restored)
            # Actual process restart reads the accepted scope and its evidence.
            code="from combat.agent_store import AgentStore; import sys,json; x=AgentStore(sys.argv[1]).retrieve(sys.argv[2]); print(json.dumps({'claims':len(x['local_claims']),'revision':x['experience_revision']}))"
            output=await asyncio.to_thread(subprocess.check_output,[sys.executable,"-c",code,str(path),build.calibration_key],text=True,
                                          cwd=Path(__file__).resolve().parents[1])
            self.assertEqual(json.loads(output)["claims"],1)
            before=next_learning.gateway.store.retrieve(build.calibration_key)["experience_revision"]
            await next_learning.record_room(fresh,restored,{"result":"CLEARED"})
            self.assertEqual(next_learning.gateway.store.retrieve(build.calibration_key)["experience_revision"],before)
            real_learning=ItemLearning(KnowledgeGateway(package,AgentStore(path)),source_mode="normal_play")
            self.assertEqual(real_learning.restore(restored)["calibration"],[])
            claim=next_learning.gateway.store.retrieve(build.calibration_key)["local_claims"][0]
            next_learning.gateway.store.contradict(claim["claim_id"],{"reason":"prediction_residual"})
            reverted=await next_learning.apply(fresh,resolver,restored)
            self.assertNotEqual(reverted.calibration_source,"experience")
            # A simulated counterexample cannot block real-mode evidence.
            measured=build.model_copy(update={"calibrated":True,"calibration_source":"observed","calibration_samples":3,"tear_speed":10.,"calibration_observations":[{"sample_id":f"real:{i}","value":10.} for i in range(3)]})
            await real_learning.record_room(fresh,measured,{"result":"CLEARED"})
            self.assertEqual(len(real_learning.restore(measured)["calibration"]),1)
