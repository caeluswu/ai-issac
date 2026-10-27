import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
import httpx
from facade import SocketBridge
from campaign.budget import RequestBudget, BudgetExceeded, diagnostic_request
from campaign.director import FloorDirector
from campaign.graph import FloorGraph, TreasureFirstRoutePolicy
from campaign.judge import FloorJudge
from campaign.models import RoomKey
from campaign.planning import FloorPlanner
from combat.builds import BuildResolver
from combat.catalog import default_plan
from combat.models import ContextRef, FloorObservation, FloorProgress, ExitObservation, Vec
from combat.planner import DeepSeekClient, Planner
from combat.runtime import CombatSession, RunLog
from tests.combat_fixtures import snapshot, context
from tests.test_combat_session import SimulatedRoom


def floor_snapshot(index=84,frame=10,boss=False,clear=True,stage=1):
    s = snapshot(frame=frame,enemies=not clear)
    s.room.update(room_idx=index,room_type=5 if boss else 1,room_shape=1,
        stage=stage,stage_type=0,is_starting_room=index == 84)
    s.room_visit_id = f"test:visit:{stage}:{index}"
    s.floor = FloorObservation(instance_id=f"test:floor:{stage}",generation=stage,
        stage=stage,stage_type=0,dimension=0,room_list_index=index,unknowns=[])
    s.floor_progress = FloorProgress(capability_version=1,curses=0,can_pick_red=False,
        can_pick_soul=True,controls_enabled=True,extra_animation_finished=True)
    s.inventory.update(keys=1,coins=0,bombs=1)
    s.health.update(max_hearts=6)
    for e in s.enemies: e.is_boss = boss
    s.exit_observations = []
    return s


def door(target,kind=1,x=560):
    return {"target_room":target,"target_list_index":target,"target_room_type":kind,
        "is_open":True,"is_locked":False,"x":x,"y":160}


class TestFloorGraph(unittest.TestCase):
    def test_dead_end_backtracks_and_prefers_observed_boss_frontier(self):
        graph = FloorGraph()
        start = floor_snapshot();start.layout["doors"] = {"2":door(85),"0":door(86,x=0)}
        a = graph.observe(start).key
        dead = floor_snapshot(85);dead.layout["doors"] = {"0":door(84,x=0)}
        graph.confirm(a,2,dead)
        self.assertEqual(graph.choose(dead)[0],0)
        graph.confirm(RoomKey.from_snapshot(dead),0,start)
        start.layout["doors"]["0"]["target_room_type"] = 5
        self.assertEqual(graph.choose(start)[1].target_room_type,5)
        graph.failed(a,0);graph.failed(a,0)
        self.assertIsNone(graph.choose(start))

    def test_large_room_alias_and_new_floor_do_not_share_nodes(self):
        graph = FloorGraph(); s = floor_snapshot();key=graph.observe(s).key
        s.room["room_idx"] = 83;graph.observe(s)
        self.assertEqual(len(graph.nodes),1)
        self.assertEqual(graph.grid_aliases[83],key)
        with self.assertRaisesRegex(ValueError,"GRAPH_FLOOR_CHANGED"): graph.observe(floor_snapshot(stage=2))

    def test_available_treasure_precedes_boss_exit(self):
        s=floor_snapshot()
        s.layout["doors"]={"0":door(85,kind=5,x=0),"2":door(86,kind=4)}
        slot,target=FloorGraph().choose(s)
        self.assertEqual(slot,2)
        self.assertEqual(target.target_room_type,4)

    def test_unexplored_normal_frontier_precedes_boss_until_treasure_found(self):
        s=floor_snapshot()
        s.layout["doors"]={"0":door(85,kind=5,x=0),"2":door(86)}
        graph=FloorGraph()
        self.assertEqual(graph.choose(s)[1].target_room_type,1)
        origin=RoomKey.from_snapshot(s)
        corridor=floor_snapshot(86);corridor.layout["doors"]={"0":door(84,x=0),"2":door(87,kind=4)}
        graph.confirm(origin,2,corridor)
        slot,door_observation=graph.choose(corridor)
        self.assertEqual(door_observation.target_room_type,4)
        self.assertEqual(slot,2)

    def test_locked_treasure_requires_key_and_route_policy_is_replaceable(self):
        s=floor_snapshot();s.inventory["keys"]=0
        s.layout["doors"]={"0":door(85,kind=5,x=0),"2":{**door(86,kind=4),"is_locked":True,"is_open":False}}
        self.assertEqual(FloorGraph().choose(s)[1].target_room_type,5)
        s.inventory["keys"]=1
        self.assertEqual(FloorGraph().choose(s)[1].target_room_type,4)
        class BossFirst(TreasureFirstRoutePolicy):
            priorities={4:1,1:2,5:0}
        self.assertEqual(FloorGraph(BossFirst()).choose(s)[1].target_room_type,5)


class TestFloorJudge(unittest.TestCase):
    def prepare_boss(self):
        judge=FloorJudge();judge.begin(floor_snapshot())
        fight=floor_snapshot(86,20,boss=True,clear=False)
        judge.action(fight,(0,0),(1,0),1);fight.applied_action_id=1
        judge.observe(fight)
        cleared=floor_snapshot(86,21,boss=True)
        cleared.exit_observations=[ExitObservation(instance_id="trapdoor",grid_index=70,type=17,variant=0,state=1,collision=0,pos=Vec(x=300,y=160))]
        judge.observe(cleared)
        cleared=cleared.model_copy(deep=True);cleared.frame=67;judge.observe(cleared)
        return judge,cleared

    def test_room_clear_or_floor_change_alone_never_proves_success(self):
        judge=FloorJudge();judge.begin(floor_snapshot())
        judge.observe(floor_snapshot(stage=2,frame=20))
        self.assertEqual(judge.failure,"UNAUTHORIZED_FLOOR_CHANGE")
        self.assertFalse(judge.success)

    def test_boss_children_delay_completion_and_wrong_exit_is_rejected(self):
        judge,s=self.prepare_boss()
        self.assertIsNotNone(judge.completed_boss)
        s.exit_observations[0].type=18
        with self.assertRaisesRegex(ValueError,"EXIT_NOT_NORMAL_MAINLINE"): judge.arm_exit("trapdoor")
        judge=FloorJudge();judge.begin(floor_snapshot())
        fight=floor_snapshot(86,20,boss=True,clear=False);judge.observe(fight)
        child=floor_snapshot(86,80,boss=True,clear=False);child.enemies[0].is_boss=False
        judge.observe(child)
        self.assertIsNone(judge.completed_boss)

    def test_full_proof_requires_normal_exit_and_applied_next_floor_movement(self):
        judge,s=self.prepare_boss();judge.arm_exit("trapdoor")
        judge.action(s,(1,0),(0,0),2)
        s=s.model_copy(deep=True);s.frame=68;s.applied_action_id=2;judge.observe(s)
        entered=floor_snapshot(stage=2,frame=69);judge.observe(entered)
        self.assertIsNotNone(judge.departure)
        self.assertFalse(judge.success)
        for frame in (70,71):
            entered=entered.model_copy(deep=True);entered.frame=frame;judge.observe(entered)
        judge.action(entered,(1,0),(0,0),3)
        entered=entered.model_copy(deep=True);entered.frame=72;entered.player.pos.x+=4;entered.applied_action_id=3
        judge.observe(entered)
        self.assertTrue(judge.success,judge.evidence())
        entered=entered.model_copy(deep=True);entered.frame=73;entered.player.dead=True;judge.observe(entered)
        self.assertFalse(judge.success)


class TestRequestBudget(unittest.IsolatedAsyncioTestCase):
    async def test_retry_consumes_attempt_and_reserved_diagnostic_cannot_be_used_by_routine(self):
        calls=[]
        async def endpoint(request):
            calls.append(request)
            if len(calls)==1:return httpx.Response(503)
            return httpx.Response(200,json={"choices":[{"message":{"content":"{}"},"finish_reason":"stop"}],
                "usage":{"prompt_tokens":10,"completion_tokens":2}})
        original=httpx.AsyncClient
        budget=RequestBudget(max_routine=2)
        with patch.dict(os.environ,{"DEEPSEEK_API_KEY":"fixture","DEEPSEEK_MODEL":"fixture"}),patch("combat.planner.httpx.AsyncClient",side_effect=lambda **kw:original(transport=httpx.MockTransport(endpoint),**kw)):
            client=DeepSeekClient(budget=budget)
            try:
                await client.complete([])
                self.assertEqual(budget.requests,2)
                with self.assertRaises(BudgetExceeded): await client.complete([])
                token=diagnostic_request.set(True)
                try: await client.complete([])
                finally: diagnostic_request.reset(token)
                self.assertEqual(budget.diagnostic,1)
                self.assertEqual(len(calls),3)
            finally: await client.aclose()

    async def test_concurrent_reservations_and_cancel_do_not_refund_unknown_usage(self):
        budget=RequestBudget(max_requests=1)
        results=await asyncio.gather(asyncio.to_thread(budget.reserve,[]),asyncio.to_thread(budget.reserve,[]),return_exceptions=True)
        self.assertEqual(sum(isinstance(x,BudgetExceeded) for x in results),1)
        self.assertEqual(budget.requests,1)
        self.assertEqual(budget.report()["unknown_usage_attempts"],1)

    async def test_floor_planning_requests_one_final_plan_without_tools(self):
        class Client:
            calls=0
            async def complete(self,messages):
                self.calls+=1
                self.system=messages[0]["content"]
                data=json.loads(messages[1]["content"])
                ref=ContextRef.model_validate(data["context"]["context_ref"])
                return default_plan(ref).model_dump_json(),{"finish_reason":"stop"}
        client=Client()
        planner=FloorPlanner(Planner(client,None,lambda *a,**kw:None),lambda *a,**kw:None)
        s=floor_snapshot(clear=False);build=BuildResolver().update(s)
        plan=await planner.plan(s,build)
        self.assertEqual(client.calls,1)
        self.assertIn("Do not return tool_calls",client.system)
        self.assertEqual(plan.context_ref.room_visit_id,s.room_visit_id)

    async def test_template_rebinds_new_visit_but_not_new_weapon_context(self):
        class Planner:
            calls=0
            async def plan(self,s,build,revision,feedback):
                self.calls+=1
                ref=context(s);ref.build_revision=build.revision;ref.experience_revision=build.experience_revision
                return default_plan(ref)
        inner=Planner();planner=FloorPlanner(inner,lambda *a,**kw:None)
        s=floor_snapshot(clear=False);build=BuildResolver().update(s)
        await planner.plan(s,build)
        s=s.model_copy(deep=True);s.room_visit_id="test:revisit"
        result=await planner.plan(s,build)
        self.assertEqual(inner.calls,1)
        self.assertEqual(result.context_ref.room_visit_id,s.room_visit_id)
        build=build.model_copy(update={"signature":"different"})
        await planner.plan(s,build)
        self.assertEqual(inner.calls,2)


class SimulatedFloor(SimulatedRoom):
    def __init__(self,end_stage=1):
        super().__init__();self.s=floor_snapshot();self.s.mode="MANUAL"
        self.end_stage=end_stage
        self.s.layout["doors"]={"2":door(85)}
        self.visits=1
        self.boss_intro_until=None
        self.boss_intro_done=False
        self.floor_intro_until=None
        self.floor_intro_done=False

    def enter(self,index,stage=1):
        if stage == 2 and not self.floor_intro_done:
            self.floor_intro_until=time.monotonic()+3
            self.floor_intro_done=True
            self.s.paused=True
            return
        if index == 86 and not self.boss_intro_done:
            self.boss_intro_until=time.monotonic()+3
            self.boss_intro_done=True
            self.s.paused=True
            return
        self.visits+=1
        self.s=floor_snapshot(index,self.s.frame,boss=index==86,clear=index==84,stage=stage)
        self.s.room_visit_id+=f":{self.visits}"
        self.s.mode="MANUAL";self.move=self.shoot=(0,0);self.hit_ticks=0
        self.s.player.pos.x=80 if index !=84 else 280
        self.s.layout["doors"]={"2":door(index+1,5 if index==85 else 1)} if index<86 else {}

    async def frames(self):
        while True:
            await asyncio.sleep(1/30)
            if self.floor_intro_until and time.monotonic() >= self.floor_intro_until:
                self.floor_intro_until=None
                self.enter(84,stage=2)
            if self.boss_intro_until and time.monotonic() >= self.boss_intro_until:
                self.boss_intro_until=None
                self.enter(86)
            if self.s.paused:
                # J835 loading can suspend both updates and render heartbeats.
                if not self.floor_intro_until or self.floor_intro_until-time.monotonic()>2:
                    await self.send({"type":"HEARTBEAT","paused":True,"frame":self.s.frame,"room_visit_id":self.s.room_visit_id,"mode":self.s.mode})
                continue
            self.s.frame+=1
            if self.s.frame-self.last_action_frame>3:self.move=self.shoot=(0,0)
            if self.s.mode=="FORCE_AI":
                self.s.player.pos.x+=self.move[0]*4;self.s.player.pos.y+=self.move[1]*4
                self.s.player.vel=Vec(x=self.move[0]*2,y=self.move[1]*2)
            if self.shoot !=(0,0) and self.s.enemies:
                self.hit_ticks+=1;self.s.enemies[0].hp=max(0,10-self.hit_ticks)
            if self.hit_ticks>=6:
                self.s.enemies=[];self.s.room.update(enemy_count=0,is_clear=True)
                if self.s.room["room_idx"]==86:
                    self.s.layout["grid"]["71"]={"type":17,"variant":0,"state":1,"collision":0,"x":440,"y":160}
                    self.s.exit_observations=[ExitObservation(instance_id="trapdoor",grid_index=71,type=17,variant=0,state=1,collision=0,pos=Vec(x=440,y=160))]
            if self.s.floor.stage<=self.end_stage and self.s.room["room_idx"]<86 and self.s.player.pos.x>=530:
                self.enter(self.s.room["room_idx"]+1,stage=self.s.floor.stage)
            elif self.s.floor.stage<=self.end_stage and self.s.room["room_idx"]==86 and self.s.exit_observations and self.s.player.pos.distance(self.s.exit_observations[0].pos)<18:
                self.enter(84,stage=self.s.floor.stage+1)
            await self.data()


class TestFloorTCP(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_failure_blocks_an_otherwise_complete_result(self):
        s=floor_snapshot()
        session=SimpleNamespace(bridge=SimpleNamespace(on_message=lambda cb:None),action_auditors=[],
            open=AsyncMock(return_value=s),close=AsyncMock(),cleanup_errors=["mode_restore_failed"],
            observation=SimpleNamespace(latest=s),room_executor=SimpleNamespace(results=[]),
            task_judge=SimpleNamespace(health=SimpleNamespace(damage=0,bones_lost=0)),
            resources=SimpleNamespace(spent={}),pause_seconds=0,llm_configured=False,fallbacks=0,
            log=lambda *args,**kwargs:None)
        director=FloorDirector(session)
        director.start=s
        director.judge.completed_boss={"proven":True}
        director.judge.departure={"proven":True}
        director.judge.actionable={"proven":True}
        director.supervised=AsyncMock()
        report=await director.run()
        self.assertFalse(report.success)
        self.assertEqual(report.result,"CLEANUP_FAILED")
        session.cleanup_errors=[]
        session.close.side_effect=lambda:setattr(director.judge,"failure","DEAD")
        report=await director.run()
        self.assertFalse(report.success)
        self.assertEqual(report.result,"FLOOR_TERMINAL_NOT_PROVEN")

    async def test_full_floor_on_one_tcp_connection_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            game=SimulatedFloor();bridge=SocketBridge(port=0);log=RunLog(Path(directory)/"run")
            session=CombatSession(bridge,None,game,log,ready_timeout=3)
            director=FloorDirector(session,wall_limit=35)
            async def boot(_): await game.connect(bridge._server._server.sockets[0].getsockname()[1])
            try:
                report=await asyncio.wait_for(director.run(bootstrap=boot),40)
                self.assertTrue(report.success,report.model_dump())
                self.assertEqual(len(report.transitions),2)
                self.assertEqual(len(report.rooms),3)
                self.assertEqual(game.s.floor.stage,2)
                self.assertEqual(game.s.mode,"MANUAL")
                self.assertEqual(game.move,(0,0))
                self.assertTrue(all(not x["use_item"] and not x["use_bomb"] for x in game.inputs))
            finally: await game.close();log.file.close()
