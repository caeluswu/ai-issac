import asyncio
from pathlib import Path
import tempfile
import unittest
from facade import SocketBridge
from combat.lifecycle import ReceiptWindow
from combat.runtime import CombatSession, RunLog
from combat.models import FloorObservation
from tests.combat_fixtures import snapshot
from tests.test_combat_session import SimulatedRoom, FixturePlanner


class TestReceiptWindow(unittest.TestCase):
    def test_bounded_history_counts_once_and_rejects_retired_duplicates(self):
        tracker = ReceiptWindow(capacity=4)
        for i in range(1, 101): tracker.add(i)
        tracker.add(1); tracker.add(99)
        self.assertEqual(len(tracker), 100)
        self.assertLessEqual(len(tracker.seen), 4)


class TestContinuousRoomSession(unittest.IsolatedAsyncioTestCase):
    async def test_three_rooms_revisit_and_same_index_new_floor_share_one_connection(self):
        class Rooms(SimulatedRoom):
            async def frames(self):
                while True:
                    await asyncio.sleep(1/30)
                    if self.s.paused:
                        await self.send({"type": "HEARTBEAT", "paused": True, "frame": self.s.frame,
                                         "room_visit_id": self.s.room_visit_id, "mode": self.s.mode})
                        continue
                    self.s.frame += 1
                    if self.s.frame-self.last_action_frame > 3: self.move = self.shoot = (0, 0)
                    if self.shoot != (0, 0): self.hit_ticks += 1
                    if self.hit_ticks >= 3:
                        self.s.enemies = []; self.s.room.update(is_clear=True, enemy_count=0)
                    await self.data()
        with tempfile.TemporaryDirectory() as directory:
            game = Rooms(); game.s = snapshot()
            bridge = SocketBridge(port=0); log = RunLog(Path(directory)/"run")
            session = CombatSession(bridge, FixturePlanner(), game, log, ready_timeout=3)
            async def boot(_): await game.connect(bridge._server._server.sockets[0].getsockname()[1])
            async def execute():
                await session.open(bootstrap=boot)
                old_pending = None
                for index, (floor, room_index) in enumerate(((1,85),(1,86),(1,85),(2,85))):
                    s = snapshot(frame=game.s.frame+1)
                    s.room_visit_id = "test:visit:"+str(index)
                    s.room.update(room_idx=room_index, is_starting_room=False)
                    s.mode = "MANUAL"
                    s.floor = FloorObservation(instance_id=f"test:floor:{floor}", generation=floor,
                        stage=1, stage_type=0, dimension=0, room_list_index=room_index, unknowns=[])
                    game.s = s; game.hit_ticks = 0; game.move = game.shoot = (0,0)
                    await game.data()
                    fresh = await session.next_snapshot()
                    while fresh.room_visit_id != s.room_visit_id:
                        fresh = await session.next_snapshot(after=fresh.snapshot_id)
                    result = await session.execute_room(fresh)
                    self.assertEqual(result, "CLEARED")
                    self.assertTrue(bridge.connected)
                    self.assertEqual(game.s.mode, "MANUAL")
                    self.assertFalse(session.solver.failed_goals)
                    if old_pending: self.assertTrue(old_pending.cancelled())
                    if index == 0:
                        old_pending = asyncio.create_task(asyncio.sleep(30))
                        session.pending = old_pending
                        session.solver.failed_goals.append((500, 500))
                        session.on_message({"type":"ACTION_RECEIPT", "action_id":1, "accepted":False,
                                            "frame":s.frame, "room_visit_id":"old-visit"})
                        self.assertIsNone(session.rejected)
                rows = list(session.room_executor.results)
                self.assertEqual([r["first_clear"] for r in rows], [True,True,False,True])
                self.assertEqual(len(rows), 4)
            try: await asyncio.wait_for(execute(), 20)
            finally:
                await session.close(); await game.close()
                log.file.close()
