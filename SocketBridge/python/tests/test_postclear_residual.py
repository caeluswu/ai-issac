from types import SimpleNamespace
from unittest.mock import AsyncMock
import unittest
from combat.builds import BuildResolver
from combat.room_execution import RoomExecutor
from tests.combat_fixtures import snapshot, context


class TestPostClearResidual(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_host_resumes_same_room_without_resetting_budget(self):
        start=snapshot(enemies=False);late=snapshot(frame=start.frame+20)
        late.room['is_clear']=True;late.room['enemy_count']=1
        late.enemies[0].type=27;late.enemies[0].vulnerable=False
        done=snapshot(frame=late.frame+30,enemies=False)
        logs=[]
        session=SimpleNamespace(observation=SimpleNamespace(latest=start),bridge=SimpleNamespace(connected=True),
            release=AsyncMock(),set_mode=AsyncMock(),send_action=AsyncMock(),death_observed=False,
            death_event_frame=None,visited=set(),navigation_devices={},learning=None,
            log=lambda kind,**kw:logs.append(kind))
        async def next_snapshot(**kw):session.observation.latest=late;return late
        session.next_snapshot=next_snapshot
        executor=RoomExecutor(session);calls=[];scope=[]
        async def engage(s,*,configure=True):
            calls.append(configure)
            if configure:
                executor.state.judge.started=start.frame
                executor.state.judge.damage=3
                scope.extend((executor.state.judge,executor.state.supervisor))
                session.observation.latest=late
            else:
                self.assertIs(executor.state.judge,scope[0])
                self.assertIs(executor.state.supervisor,scope[1])
                self.assertEqual(executor.state.judge.started,start.frame)
                session.observation.latest=done
            return 'CLEARED'
        executor._engage=engage
        self.assertEqual(await executor.execute(start),'CLEARED')
        self.assertEqual(calls,[True,False])
        self.assertIn('post_clear_combat_resumed',logs)
        self.assertEqual(executor.results[-1]['damage'],3)
        self.assertEqual(executor.results[-1]['ticks'],done.frame-start.frame)

    async def test_far_bullet_can_expire_before_terminal_room_result(self):
        start=snapshot(enemies=False)
        start.projectiles["enemy_projectiles"]=[{"id":1,"pos":{"x":500,"y":280},"vel":{"x":5,"y":0},"collision_radius":5}]
        states=[start.model_copy(deep=True) for _ in range(6)]
        for i,s in enumerate(states):
            s.frame=start.frame+i+1
            if i>=1:s.projectiles["enemy_projectiles"]=[]
        session=SimpleNamespace(observation=SimpleNamespace(latest=start),bridge=SimpleNamespace(connected=True),
            release=AsyncMock(),set_mode=AsyncMock(),send_action=AsyncMock(),death_observed=False,
            death_event_frame=None,visited=set(),navigation_devices={},build_resolver=BuildResolver(),
            learning=None,log=lambda *a,**kw:None)
        async def next_snapshot(**kw):
            s=states.pop(0);session.observation.latest=s;return s
        session.next_snapshot=next_snapshot
        session.context_ref=lambda s,b:context(s).model_copy(update={"build_revision":b.revision,"experience_revision":b.experience_revision})
        executor=RoomExecutor(session)
        executor._engage=AsyncMock(return_value="CLEARED")
        result=await executor.execute(start)
        self.assertEqual(result,"CLEARED")
        self.assertEqual(executor.results[-1]["post_clear_result"],"SAFE")
        self.assertGreater(session.observation.latest.frame,start.frame+3)
