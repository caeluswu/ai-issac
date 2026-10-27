from pathlib import Path
import tempfile
import unittest
from combat.agent_store import AgentStore
from combat.boundary_safety import boundary_danger
from combat.builds import BuildResolver
from combat.item_knowledge import KnowledgeGateway
from combat.item_learning import ItemLearning
from combat.runtime import CombatSession, RunLog
from facade import SocketBridge
from tests.test_combat_session import SimulatedRoom
from tests.test_campaign import floor_snapshot
from tests.test_item_knowledge import package_fixture, pickup


class TestFloorPickups(unittest.TestCase):
    def test_health_capacity_resource_subtypes_and_zero_shop_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway=KnowledgeGateway(package_fixture(directory),AgentStore(Path(directory)/"agent.sqlite"))
            gateway.purchase_budget=0
            s=floor_snapshot();build=BuildResolver().update(s)
            self.assertEqual(gateway.decide(s,build,pickup(1,variant=10)).decision,"skip")
            s.floor_progress.can_pick_red=True;s.health["red_hearts"]=3
            self.assertEqual(gateway.decide(s,build,pickup(1,variant=10)).decision,"take")
            self.assertEqual(gateway.decide(s,build,pickup(3,variant=10)).decision,"take")
            self.assertEqual(gateway.decide(s,build,pickup(6,variant=10)).decision,"skip")
            self.assertEqual(gateway.decide(s,build,pickup(4,variant=40)).decision,"skip")
            self.assertEqual(gateway.decide(s,build,pickup(1,variant=40)).decision,"take")
            s.inventory["bombs"]=99
            self.assertEqual(gateway.decide(s,build,pickup(1,variant=40)).decision,"skip")
            s.inventory["coins"]=99
            self.assertEqual(gateway.decide(s,build,pickup(1,price=1)).decision,"skip")
            self.assertEqual(gateway.decide(s,build,pickup(1,options_pickup_index=1)).decision,"skip")

    def test_distant_static_fire_allows_a_boundary_but_near_fire_or_projectiles_do_not(self):
        s=floor_snapshot()
        s.hazards=[{"type":"FIREPLACE","is_shooting":False,"pos":{"x":100,"y":160},"collision_radius":13}]
        self.assertFalse(boundary_danger(s))
        s.hazards[0]["pos"]["x"]=280
        self.assertTrue(boundary_danger(s))
        s.hazards=[];s.projectiles["enemy_projectiles"]=[{"id":1}]
        self.assertTrue(boundary_danger(s))


class TestHealthPickupTCP(unittest.IsolatedAsyncioTestCase):
    async def test_red_heart_confirmation_uses_health_gain_and_consumed_entity(self):
        class Hearts(SimulatedRoom):
            async def data(self):
                if self.s.pickups and self.s.player.pos.distance(self.s.pickups[0].pos)<18:
                    self.s.pickups=[]
                    self.s.health["red_hearts"]+=2
                await super().data()
        with tempfile.TemporaryDirectory() as directory:
            learning=ItemLearning(KnowledgeGateway(package_fixture(directory),AgentStore(Path(directory)/"agent.sqlite")),source_mode="simulated_game")
            game=Hearts();game.s=floor_snapshot();game.s.health["red_hearts"]=3
            game.s.floor_progress.can_pick_red=True
            game.s.pickups=[pickup(1,variant=10)]
            bridge=SocketBridge(port=0);log=RunLog(Path(directory)/"run")
            session=CombatSession(bridge,None,game,log,ready_timeout=3,learning=learning)
            async def boot(_):await game.connect(bridge._server._server.sockets[0].getsockname()[1])
            try:
                s=await session.open(bootstrap=boot)
                build=session.build_resolver.update(s);p=s.pickups[0]
                decision=learning.gateway.decide(s,build,p)
                result=await learning.pickups.execute(session,s,build,p,decision)
                self.assertEqual(result["result"],"confirmed",result)
                self.assertEqual(game.s.health["red_hearts"],5)
            finally:await session.close();await game.close();log.file.close()
