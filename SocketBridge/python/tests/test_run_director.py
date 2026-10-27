import asyncio
from pathlib import Path
import tempfile
import unittest
from facade import SocketBridge
from campaign.judge import FloorJudge
from campaign.run_director import RunDirector
from combat.runtime import CombatSession,RunLog
from tests.test_campaign import SimulatedFloor,floor_snapshot


class TestMultiFloorJudge(unittest.TestCase):
    def test_second_floor_requires_matching_start_and_third_floor_exit(self):
        judge=FloorJudge(expected_stage=2)
        with self.assertRaisesRegex(ValueError,"UNSUPPORTED_FLOOR_START"):
            judge.begin(floor_snapshot(stage=1))
        judge.begin(floor_snapshot(stage=2))
        judge.observe(floor_snapshot(stage=4,frame=20))
        self.assertEqual(judge.failure,"UNAUTHORIZED_FLOOR_CHANGE")


class TestTwoFloorTCP(unittest.IsolatedAsyncioTestCase):
    async def test_same_connection_and_run_clear_two_floors(self):
        with tempfile.TemporaryDirectory() as directory:
            game=SimulatedFloor(end_stage=2)
            bridge=SocketBridge(port=0)
            log=RunLog(Path(directory)/"run")
            session=CombatSession(bridge,None,game,log,ready_timeout=3)
            director=RunDirector(session,target_stage=2,wall_limit=65)
            async def boot(_):
                await game.connect(bridge._server._server.sockets[0].getsockname()[1])
            try:
                report=await asyncio.wait_for(director.run(bootstrap=boot),70)
                self.assertTrue(report.success,report.model_dump())
                self.assertEqual([f.context.stage for f in report.floors],[1,2])
                self.assertEqual(len(set(report.floor_instance_ids)),2)
                self.assertEqual(len({f.context.run_id for f in report.floors}),1)
                self.assertEqual(game.s.floor.stage,3)
                self.assertEqual(game.s.mode,"MANUAL")
                self.assertEqual(game.move,(0,0))
                self.assertEqual(sum(len(f.transitions) for f in report.floors),4)
            finally:
                await game.close()
                log.file.close()
