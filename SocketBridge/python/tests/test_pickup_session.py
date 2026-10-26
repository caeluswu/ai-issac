import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from facade import SocketBridge
from combat.runtime import CombatSession, RunLog
from tests.test_combat_session import SimulatedRoom, FixturePlanner


class TestPickupSession(unittest.IsolatedAsyncioTestCase):
    async def test_supported_item_change_during_combat_replaces_stale_plan(self):
        class PickupRoom(SimulatedRoom):
            async def data(self):
                if self.s.room_visit_id == "test:2" and self.hit_ticks >= 1:
                    self.s.inventory["collectibles"]["4"] = 1
                    self.s.stats["damage"] = 5.
                await super().data()
        with tempfile.TemporaryDirectory() as directory:
            game = PickupRoom(hit_threshold=12)
            bridge = SocketBridge(port=0); log = RunLog(Path(directory)/"run")
            session = CombatSession(bridge, FixturePlanner(), game, log, ready_timeout=3)
            async def boot(_): await game.connect(bridge._server._server.sockets[0].getsockname()[1])
            try:
                result = await asyncio.wait_for(session.run(bootstrap=boot), 20)
                self.assertTrue(result.success, result.model_dump())
                rows = [json.loads(x) for x in (log.directory/"trace.jsonl").read_text().splitlines()]
                changes = [r for r in rows if r["kind"] == "build_transaction"]
                self.assertTrue(changes)
                self.assertGreater(changes[0]["revision"], 1)
                self.assertEqual(game.s.mode, "MANUAL")
            finally: await game.close()
