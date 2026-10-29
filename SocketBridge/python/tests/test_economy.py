import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from combat.agent_store import AgentStore
from combat.builds import BuildResolver
from combat.economy import PickupChoicePolicy
from combat.geometry import GeometryService
from combat.item_knowledge import KnowledgeGateway
from combat.item_learning import ItemLearning
from campaign.graph import FloorGraph
from campaign.director import FloorDirector
from tests.test_campaign import floor_snapshot, door
from tests.test_item_knowledge import package_fixture, pickup
from tests.test_combat_session import SimulatedRoom
from facade import SocketBridge
from combat.runtime import CombatSession, RunLog
from combat.controller import JointActionSolver
from combat.catalog import default_plan
from tests.combat_fixtures import context


class TestEconomy(unittest.TestCase):
    def test_known_boss_beats_optional_rooms_after_treasure_but_not_before(self):
        s=floor_snapshot();s.layout['doors']={'0':door(85),'2':door(86,kind=5)}
        graph=FloorGraph()
        self.assertEqual(graph.choose(s)[1].target_room_type,1)
        treasure=floor_snapshot(87);treasure.room['room_type']=4
        graph.observe(treasure)
        self.assertEqual(graph.choose(s)[1].target_room_type,5)
        s.inventory['coins']=15;s.layout['doors']['1']=door(88,kind=2)
        self.assertEqual(graph.choose(s)[1].target_room_type,2)

    def test_shop_threshold_includes_exactly_fifteen_and_reserves_key(self):
        s=floor_snapshot();s.layout['doors']={'0':door(85,kind=4),'2':door(86,kind=2)}
        for coins,expected in ((14,4),(15,2),(16,2)):
            s.inventory['coins']=coins
            self.assertEqual(FloorGraph().choose(s)[1].target_room_type,expected)
        s.layout['doors']['2'].update(is_locked=True,is_open=False)
        self.assertEqual(FloorGraph().choose(s)[1].target_room_type,4)
        s.inventory['keys']=2
        self.assertEqual(FloorGraph().choose(s)[1].target_room_type,2)

    def test_new_passive_builds_keep_normal_input_and_observed_damage(self):
        for item in (64,199,203,208,227,249,260,414,535,182,185,243,374,407,574,686,499):
            s=floor_snapshot();s.inventory['collectibles'][str(item)]=1;s.stats['damage']=7.25
            build=BuildResolver().update(s)
            self.assertEqual(build.family,'normal_tears',item)
            self.assertEqual(build.damage_per_hit,7.25,item)

    def test_known_shop_health_can_be_revisited_below_fifteen_coins(self):
        graph=FloorGraph();s=floor_snapshot();s.layout['doors']={'2':door(85,kind=2)}
        source=graph.observe(s).key
        shop=floor_snapshot(85);shop.room['room_type']=2
        shop.pickups=[pickup(3,variant=10,price=5)]
        shop.layout['doors']={'0':door(84)}
        graph.confirm(source,2,shop)
        s.inventory['coins']=5;s.health['red_hearts']=1
        choice=graph.choose(s)
        self.assertIsNotNone(choice)
        self.assertEqual(choice[1].target_room_type,2)

    def test_spectral_rock_permission_does_not_allow_player_collision(self):
        s=floor_snapshot();index=70;cell=s.layout['cells'][str(index)]
        cell['collision']=4
        s.layout['grid'][str(index)]={'type':2,'variant':0,'state':0,'collision':4,'x':cell['x'],'y':cell['y']}
        self.assertNotIn(index,GeometryService(s).shootable)
        s.inventory['collectibles']['185']=1
        geometry=GeometryService(s)
        self.assertIn(index,geometry.shootable)
        self.assertNotIn(index,geometry.walkable)

    def test_emergency_health_outranks_generic_item(self):
        s=floor_snapshot();s.health['red_hearts']=1
        policy=PickupChoicePolicy()
        heart=pickup(3,variant=10,price=5)
        item=pickup(1,price=15,instance_id='other')
        self.assertIs(policy.choose(s,[(item,None),(heart,None)])[0],heart)

    def test_spectral_pickup_invalidates_geometry_when_already_flying(self):
        s=floor_snapshot();s.stats['can_fly']=True;s.inventory['collectibles']['20']=1
        solver=JointActionSolver();resolver=BuildResolver();build=resolver.update(s)
        solver.solve(s,default_plan(context(s)),build)
        previous=solver.geometry
        s.inventory['collectibles']['185']=1;build=resolver.update(s)
        ref=context(s).model_copy(update={'build_revision':build.revision})
        solver.solve(s,default_plan(ref),build)
        self.assertIsNot(solver.geometry,previous)

    def test_buy_health_respects_capacity_price_and_room(self):
        with tempfile.TemporaryDirectory() as root:
            gateway=KnowledgeGateway(package_fixture(root),AgentStore(Path(root)/'agent.sqlite'))
            s=floor_snapshot();s.room['room_type']=2;s.inventory['coins']=5
            heart=pickup(3,variant=10,price=5)
            build=BuildResolver().update(s)
            self.assertEqual(gateway.decide(s,build,heart).cost_coins,5)
            s.floor_progress.can_pick_soul=False
            self.assertEqual(gateway.decide(s,build,heart).decision,'skip')
            s.floor_progress.can_pick_soul=True;s.inventory['coins']=4
            self.assertEqual(gateway.decide(s,build,heart).decision,'skip')
            s.inventory['coins']=5;s.room['room_type']=1
            self.assertEqual(gateway.decide(s,build,heart).decision,'skip')


class TestEconomyAsync(unittest.IsolatedAsyncioTestCase):
    async def test_purchase_with_simultaneous_coin_gain_is_not_repeated(self):
        class Buys(SimulatedRoom):
            async def data(self):
                if self.s.pickups and self.s.player.pos.distance(self.s.pickups[0].pos)<18:
                    self.s.pickups=[]
                    self.s.inventory['collectibles']['1']=1
                    self.s.inventory['coins']-=12  # Price 15 and immediate gain 3.
                await super().data()
        with tempfile.TemporaryDirectory() as root:
            learning=ItemLearning(KnowledgeGateway(package_fixture(root),AgentStore(Path(root)/'agent.sqlite')))
            game=Buys();game.s=floor_snapshot();game.s.room['room_type']=2
            game.s.inventory['coins']=15;game.s.pickups=[pickup(1,price=15)]
            bridge=SocketBridge(port=0);log=RunLog(Path(root)/'run')
            session=CombatSession(bridge,None,game,log,ready_timeout=3,learning=learning)
            async def boot(_):await game.connect(bridge._server._server.sockets[0].getsockname()[1])
            try:
                s=await session.open(bootstrap=boot)
                p=s.pickups[0];build=session.build_resolver.update(s)
                decision=learning.gateway.decide(s,build,p)
                result=await learning.pickups.execute(session,s,build,p,decision)
                self.assertEqual(result['result'],'confirmed',result)
                self.assertEqual(result['observed_net_spend'],12)
                self.assertEqual(result['cost_evidence'],'simultaneous_income_unresolved')
            finally:await session.close();await game.close();log.file.close()

    async def test_group_comparison_authorizes_only_one_candidate(self):
        with tempfile.TemporaryDirectory() as root:
            gateway=KnowledgeGateway(package_fixture(root),AgentStore(Path(root)/'agent.sqlite'))
            learning=ItemLearning(gateway)
            s=floor_snapshot();s.pickups=[pickup(1,options_pickup_index=7),
                pickup(14,options_pickup_index=7,instance_id='second')]
            build=BuildResolver().update(s)
            await learning.prepare_room(s,build)
            self.assertEqual(sum(d.decision=='take' for d in learning.decisions.values()),1)

    async def test_angel_excursion_uses_negative_identity_and_returns_to_boss(self):
        s=floor_snapshot(90,boss=True);s.layout['doors']={'0':door(-1,kind=15)}
        angel=floor_snapshot(-1);angel.room['room_type']=15
        angel.layout['doors']={'2':door(90,kind=5)}
        returned=s.model_copy(deep=True);returned.room_visit_id+=':return'
        director=FloorDirector.__new__(FloorDirector)
        log=lambda *a,**k:None
        director.session=SimpleNamespace(navigate_door=AsyncMock(side_effect=[(angel,None),(returned,None)]),
            log=log,learning=SimpleNamespace(collect=AsyncMock(side_effect=lambda session,value:value)),build_resolver=BuildResolver())
        director.angel_excursions=set();director.rooms=[];director.judge=SimpleNamespace(visits={})
        director.acquire=AsyncMock(side_effect=lambda value:value)
        director.settle=AsyncMock(side_effect=lambda value,*args:value)
        director.enter_state=log
        final=await director.visit_angel(s)
        self.assertEqual(final.room_visit_id,returned.room_visit_id)
        self.assertEqual(len(director.rooms),1)
        self.assertEqual([call.args[1].room_visit_id for call in director.session.learning.collect.await_args_list],
                         [angel.room_visit_id,returned.room_visit_id])
        await director.visit_angel(final)
        self.assertEqual(director.session.navigate_door.await_count,2)
