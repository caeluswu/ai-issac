import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from combat.agent_store import AgentStore
from combat.catalog import default_plan
from combat.controller import BuildResolver,JointActionSolver
from combat.item_interaction import pickup_approach_allowed
from combat.item_knowledge import KnowledgeGateway
from combat.item_learning import ItemLearning
from combat.models import WorldSnapshot,Vec
from tests.combat_fixtures import snapshot,context
from tests.test_campaign import floor_snapshot
from tests.test_item_knowledge import package_fixture,pickup


class TestExitPrecision(unittest.TestCase):
    def test_captured_trapdoor_pose_is_not_already_an_interaction(self):
        s=WorldSnapshot.model_validate(json.loads((Path(__file__).parent/'fixtures/floor5_exit_stall.json').read_text(encoding='utf8')))
        exit=s.exit_observations[0];build=BuildResolver().update(s)
        solver=JointActionSolver()
        a=solver.solve(s,default_plan(context(s)),build,True,exit.pos,(),(exit.grid_index,),goal_tolerance=1.)
        self.assertNotEqual(a.move,(0,0))
        self.assertGreater(a.diagnostics['route_remaining'],1)
        selected=next(c for c in solver.last_candidates if c['move']==a.move)
        p=Vec(x=selected['predicted_next_position'][0],y=selected['predicted_next_position'][1])
        self.assertLess(p.distance(exit.pos),s.player.pos.distance(exit.pos))


class TestNonblockingPickup(unittest.IsolatedAsyncioTestCase):
    async def test_clear_room_with_distant_nonblocking_actor_can_collect(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway=KnowledgeGateway(package_fixture(directory),AgentStore(Path(directory)/'agent.sqlite'))
            learning=ItemLearning(gateway)
            s=floor_snapshot();hazard=snapshot().enemies[0]
            hazard.type=44;hazard.can_shut_doors=False;hazard.pos=Vec(x=80,y=80)
            s.enemies=[hazard];s.pickups=[pickup(1)]
            resolver=BuildResolver()
            approaching=s.model_copy(deep=True);approaching.frame+=1
            acquired=approaching.model_copy(deep=True);acquired.frame+=1
            acquired.inventory['collectibles']['1']=1;acquired.pickups=[]
            finished=acquired.model_copy(deep=True);finished.frame+=1
            session=SimpleNamespace(build_resolver=resolver,release=AsyncMock(),set_mode=AsyncMock(),
                observation=SimpleNamespace(latest=s),bridge=SimpleNamespace(connected=True),
                next_snapshot=AsyncMock(side_effect=[approaching,acquired,finished]),send_action=AsyncMock(),
                context_ref=lambda value,build:context(value),log=lambda *a,**k:None)
            result=await learning.collect(session,s)
            self.assertEqual(result.inventory['collectibles']['1'],1)
            self.assertEqual(session.send_action.await_count,1)
            self.assertEqual(session.send_action.await_args.args[2],(0,0))

    async def test_actor_at_target_still_blocks_pickup(self):
        s=floor_snapshot();hazard=snapshot().enemies[0]
        hazard.can_shut_doors=False;s.pickups=[pickup(1)];hazard.pos=s.pickups[0].pos
        s.enemies=[hazard]
        self.assertFalse(pickup_approach_allowed(s,s.pickups[0]))
