"""End-to-end probe tests with a simulated game TCP client."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from apps.control_probe import ControlProbe
from facade import SocketBridge


class FakeGame:
    def __init__(self):
        self.frame = 0
        self.room = 1
        self.x = 100.0
        self.y = 100.0
        self.vx = 0.0
        self.vy = 0.0
        self.mode = "AUTO"
        self.move = (0, 0)
        self.shoot = (0, 0)
        self.tear_id = 0
        self.sent_inputs = []
        self.sent_modes = []
        self.last_input_frame = None
        self.pause_frames = False
        self.auto_door = False
        self.writer = None
        self.tasks = []

    async def connect(self, port: int):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)
        self.tasks = [asyncio.create_task(self._receive()), asyncio.create_task(self._frames())]

    async def _receive(self):
        while line := await self.reader.readline():
            command = json.loads(line)
            if command.get("command") == "SET_CONTROL_MODE":
                self.mode = command["params"]["mode"]
                self.move = (0, 0)
                self.shoot = (0, 0)
                self.sent_modes.append(self.mode)
                await self._send({"type": "CMD", "frame": self.frame,
                                  "result": {"success": True, "mode": self.mode}})
            elif command.get("command") == "EXEC_CONSOLE":
                self.room = 2
                await self._send({"type": "CMD", "frame": self.frame,
                                  "result": {"success": True,
                                             "command": command["params"]["command"]}})
            elif "move" in command or "shoot" in command:
                self.sent_inputs.append(command)
                self.move = (command["move"]["x"], command["move"]["y"])
                self.shoot = (command["shoot"]["x"], command["shoot"]["y"])
                self.last_input_frame = self.frame

    async def _send(self, data):
        self.writer.write((json.dumps(data) + "\n").encode("utf-8"))
        await self.writer.drain()

    async def _frames(self):
        try:
            while True:
                await asyncio.sleep(1 / 30)
                if self.pause_frames:
                    continue
                self.frame += 1
                if self.last_input_frame is not None and self.frame - self.last_input_frame > 3:
                    self.move = (0, 0)
                    self.shoot = (0, 0)
                    self.last_input_frame = None
                self.vx = self.move[0] * 4.0 if self.mode == "FORCE_AI" else 0.0
                self.vy = self.move[1] * 4.0 if self.mode == "FORCE_AI" else 0.0
                self.x += self.vx
                self.y += self.vy
                if self.auto_door and self.room == 1 and self.x >= 300:
                    self.room = 2
                    self.mode = "MANUAL"
                    self.move = (0, 0)
                tears = []
                if self.mode == "FORCE_AI" and self.shoot != (0, 0) and self.frame % 5 == 0:
                    self.tear_id += 1
                    tears = [{"id": self.tear_id, "pos": {"x": self.x, "y": self.y}}]
                await self._send({
                    "version": "3.0", "type": "DATA", "seq": self.frame,
                    "frame": self.frame, "room_index": self.room,
                    "channels": ["PLAYER_POSITION", "PROJECTILES", "ROOM_INFO",
                                 "ROOM_LAYOUT"],
                    "payload": {
                        "PLAYER_POSITION": [{"pos": {"x": self.x, "y": self.y},
                                             "vel": {"x": self.vx, "y": self.vy}}],
                        "PROJECTILES": {"player_tears": tears, "enemy_projectiles": [],
                                        "lasers": []},
                        "ROOM_INFO": {"room_idx": self.room, "is_clear": True},
                        "ROOM_LAYOUT": {
                            "grid": [],
                            "doors": ({"2": {"target_room": 2, "is_open": True,
                                             "is_locked": False, "x": 300, "y": 100}}
                                      if self.room == 1 else []),
                            "grid_size": 169, "width": 13, "height": 13,
                        },
                    },
                })
        except (asyncio.CancelledError, ConnectionError):
            pass

    async def close(self):
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except ConnectionError:
                pass
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


class TestControlProbe(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bridge = SocketBridge(port=0)
        await self.bridge.start()
        port = self.bridge._server._server.sockets[0].getsockname()[1]
        self.game = FakeGame()
        await self.game.connect(port)
        self.probe = ControlProbe(self.bridge, Path(self.temp.name), cycles=1,
                                  phase_scale=0.65)
        await self.probe.wait_ready(timeout=2.0)

    async def asyncTearDown(self):
        await self.game.close()
        await self.probe.cleanup()
        if not self.probe._trace.closed:
            self.probe.finish(False, None)
        self.temp.cleanup()

    async def test_control_sequence_mode_ack_and_room_transition(self):
        await self.probe.set_mode("FORCE_AI")
        await self.probe.run_motion_sequence()
        self.assertEqual(len(self.probe.results), 6)
        self.assertTrue(all(result["passed"] for result in self.probe.results))
        self.assertTrue((await self.probe.run_watchdog())["passed"])
        self.assertTrue(self.game.sent_inputs)
        self.assertTrue(all("move" in cmd and "shoot" in cmd for cmd in self.game.sent_inputs))
        await self.probe.set_mode("MANUAL")
        self.assertTrue(await self.probe.trigger_room_change("goto s.error.21", timeout=1.0))
        await self.probe.cleanup()
        summary = self.probe.finish(True, True)
        self.assertTrue(summary["control_chain_passed"])
        self.assertFalse(summary["passed"])
        self.assertEqual(summary["room_transition_method"], "console")
        self.assertEqual(self.game.sent_modes, ["FORCE_AI", "MANUAL", "MANUAL"])
        self.assertTrue(self.probe.summary_path.exists())

    async def test_stale_observation_and_disconnect_abort(self):
        await self.probe.set_mode("FORCE_AI")
        self.game.pause_frames = True
        await asyncio.sleep(0.55)
        self.assertFalse(self.probe.fresh_position())
        with self.assertRaises(TimeoutError):
            await self.probe.run_phase(1, "move_right", (1, 0), (0, 0), 0.2)
        self.game.pause_frames = False
        await self.probe.wait_ready(timeout=1.0)
        await self.game.close()
        for _ in range(20):
            if not self.bridge.connected:
                break
            await asyncio.sleep(0.02)
        self.assertFalse(self.bridge.connected)
        self.assertFalse(self.probe.fresh_position())
        with self.assertRaises(TimeoutError):
            await self.probe.run_phase(1, "move_right", (1, 0), (0, 0), 0.2)

    async def test_invalid_mode_rejected_before_send(self):
        with self.assertRaises(ValueError):
            await self.bridge.set_control_mode("UNKNOWN")
        self.assertEqual(self.game.sent_modes, [])

    async def test_ordinary_movement_crosses_right_door(self):
        self.game.auto_door = True
        await self.probe.set_mode("FORCE_AI")
        self.assertTrue(await self.probe.enter_right_door(timeout=3.0))
        self.assertEqual(self.probe.latest_room, 2)
        self.assertEqual(self.probe.room_transition_method, "door")


if __name__ == "__main__":
    unittest.main()
