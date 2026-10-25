"""Real TCP orchestration with a simulated game; does not prove gameplay skill."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from facade import SocketBridge
from combat.catalog import default_plan
from combat.runtime import CombatSession, RunLog
from tests.combat_fixtures import snapshot, context


class SimulatedRoom:
    def __init__(self, hit_threshold=8):
        self.s = snapshot(enemies=False)
        self.s.mode = "MANUAL"
        self.move = (0, 0)
        self.shoot = (0, 0)
        self.tasks = []
        self.hit_ticks = 0
        self.last_action_frame = -100
        self.clear = False
        self.hit_threshold = hit_threshold
        self.inputs = []

    def focused(self): return True

    def release_focus(self): pass

    async def toggle(self):
        self.s.paused = not self.s.paused

    async def connect(self, port):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)
        self.tasks = [asyncio.create_task(self.receive()), asyncio.create_task(self.frames())]

    async def send(self, raw):
        self.writer.write((json.dumps(raw)+"\n").encode()); await self.writer.drain()

    async def data(self):
        await self.send({"type": "DATA", "frame": self.s.frame, "room_index": self.s.room["room_idx"],
                         "payload": {"COMBAT_FRAME": self.s.model_dump()}, "channels": ["COMBAT_FRAME"]})

    async def receive(self):
        while line := await self.reader.readline():
            cmd = json.loads(line)
            if cmd.get("command"):
                result = {"success": True}
                if cmd["command"] == "SET_CONTROL_MODE":
                    self.s.mode = cmd["params"]["mode"]; result["mode"] = self.s.mode
                    self.move = self.shoot = (0, 0)
                if cmd["command"] == "GET_FULL_STATE": await self.data()
                await self.send({"type": "CMD", "request_id": cmd["request_id"], "result": result})
            elif "move" in cmd:
                self.inputs.append(cmd)
                accepted = (self.s.mode == "FORCE_AI" and not self.s.paused and
                            cmd.get("room_visit_id") == self.s.room_visit_id and self.s.frame-cmd.get("observed_frame", -100) <= 2)
                if "action_id" in cmd:
                    await self.send({"type": "ACTION_RECEIPT", "action_id": cmd["action_id"], "accepted": accepted,
                                     "frame": self.s.frame, "room_visit_id": self.s.room_visit_id})
                else: accepted = True
                if accepted:
                    self.move = tuple(cmd["move"][k] for k in ("x", "y"))
                    self.shoot = tuple(cmd["shoot"][k] for k in ("x", "y"))
                    self.last_action_frame = self.s.frame
                    self.s.applied_action_id = cmd.get("action_id")
                    self.s.applied_frame = self.s.frame

    async def frames(self):
        while True:
            await asyncio.sleep(1/30)
            if self.s.paused:
                await self.send({"type": "HEARTBEAT", "paused": True, "frame": self.s.frame,
                                 "room_visit_id": self.s.room_visit_id, "mode": self.s.mode})
                continue
            self.s.frame += 1
            if self.s.frame-self.last_action_frame > 3: self.move = self.shoot = (0, 0)
            if self.s.mode == "FORCE_AI":
                self.s.player.pos.x += self.move[0]*4
                self.s.player.pos.y += self.move[1]*4
            if self.s.room_visit_id == "test:1" and self.s.player.pos.x >= 530:
                frame = self.s.frame
                self.s = snapshot(frame=frame)
                self.s.room_visit_id = "test:2"; self.s.room["room_idx"] = 85
                self.s.room["is_starting_room"] = False; self.s.mode = "MANUAL"
                self.move = self.shoot = (0, 0)
            if self.s.room_visit_id == "test:2" and self.shoot != (0, 0): self.hit_ticks += 1
            if self.hit_ticks >= self.hit_threshold:
                self.s.room.update(is_clear=True, enemy_count=0); self.s.enemies = []
            await self.data()

    async def close(self):
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if hasattr(self, "writer"):
            self.writer.close(); await self.writer.wait_closed()


class FixturePlanner:
    def __init__(self, failure=False): self.failure = failure
    async def plan(self, s, build, revision=0, feedback=None):
        if self.failure: raise TimeoutError("fixture API timeout")
        await asyncio.sleep(.03)
        ref = context(s); ref.plan_revision = revision
        return default_plan(ref)


class TestSession(unittest.IsolatedAsyncioTestCase):
    async def test_resume_retries_only_while_owned_pause_is_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            game = SimulatedRoom(); log = RunLog(Path(directory)/"run")
            session = CombatSession(SocketBridge(port=0), None, game, log)
            frozen = snapshot(); frozen.paused = True
            session.observation.latest = frozen
            session.pause_owned = True
            session.heartbeat = {"paused": True}
            import time
            session.heartbeat_at = time.monotonic()
            calls = []
            async def ignore_first():
                calls.append(1)
                session.heartbeat_at = time.monotonic()
                if len(calls) == 2:
                    session.observation.latest = frozen.model_copy(update={"frame": frozen.frame+1, "paused": False})
            game.toggle = ignore_first
            await session.resume_owned_pause(frozen)
            self.assertEqual(len(calls), 2)
            self.assertFalse(session.pause_owned)
            log.file.close()

    async def test_replanning_installs_at_action_boundary_while_control_continues(self):
        class SlowReplanner(FixturePlanner):
            def __init__(self): self.calls = 0; self.failure = False
            async def plan(self, *args, **kwargs):
                self.calls += 1
                if self.calls > 1: await asyncio.sleep(.2)
                return await super().plan(*args, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            bridge = SocketBridge(port=0)
            game = SimulatedRoom(hit_threshold=120)
            log = RunLog(Path(directory)/"run")
            planner = SlowReplanner()
            session = CombatSession(bridge, planner, game, log, ready_timeout=3)
            async def boot(_):
                await game.connect(bridge._server._server.sockets[0].getsockname()[1])
            try:
                report = await asyncio.wait_for(session.run(bootstrap=boot), 15)
                self.assertTrue(report.success, report.error)
                records = [json.loads(line) for line in (log.directory/"trace.jsonl").read_text(encoding="utf-8").splitlines()]
                start = next(i for i, row in enumerate(records) if row["kind"] == "replan_started")
                installed = next(i for i, row in enumerate(records) if row["kind"] == "plan_installed" and row["source"] == "deepseek_replan")
                self.assertTrue(any(row["kind"] == "action_sent" for row in records[start+1:installed]))
                self.assertGreaterEqual(planner.calls, 2)
            finally: await game.close()

    async def run_session(self, failure=False):
        with tempfile.TemporaryDirectory() as directory:
            bridge = SocketBridge(port=0)
            game = SimulatedRoom()
            log = RunLog(Path(directory)/"run")
            session = CombatSession(bridge, FixturePlanner(failure), game, log, ready_timeout=3)
            task = asyncio.create_task(session.run())
            try:
                while not bridge._server._server: await asyncio.sleep(.01)
                await game.connect(bridge._server._server.sockets[0].getsockname()[1])
                report = await asyncio.wait_for(task, 15)
                self.assertTrue(report.success, report.model_dump())
                self.assertTrue(report.completed_door_transition)
                self.assertGreater(report.accepted_actions, 0)
                self.assertGreater(report.observed_applied_actions, 0)
                self.assertEqual(report.damage_half_hearts, 0)
                self.assertEqual(game.s.mode, "MANUAL")
                self.assertTrue(all(not c["use_bomb"] and not c["use_item"] for c in game.inputs))
                self.assertTrue(Path(directory, "run/report.json").exists())
                return report
            finally:
                task.cancel(); await asyncio.gather(task, return_exceptions=True)
                await game.close()

    async def test_exit_pause_plan_fight_and_cleanup(self):
        report = await self.run_session()
        self.assertTrue(report.llm_configured)
        self.assertEqual(report.fallback_count, 0)

    async def test_api_timeout_uses_explicit_local_fallback(self):
        report = await self.run_session(failure=True)
        self.assertFalse(report.llm_configured)
        self.assertEqual(report.fallback_count, 1)

    async def test_bootstrap_runs_after_listen_and_failure_stops_server(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = SocketBridge(port=0)
            log = RunLog(Path(directory)/"run")
            session = CombatSession(bridge, None, SimulatedRoom(), log)
            async def fail(current):
                self.assertIs(current, session)
                self.assertTrue(bridge._server.running)
                raise RuntimeError("fixture startup failure")
            report = await session.run(bootstrap=fail)
            self.assertIn("fixture startup failure", report.error)
            self.assertFalse(bridge._server.running)
            self.assertTrue((log.directory/"report.json").is_file())

    async def test_startup_only_confirms_pause_without_combat(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = SocketBridge(port=0)
            game = SimulatedRoom()
            log = RunLog(Path(directory)/"run")
            session = CombatSession(bridge, None, game, log, ready_timeout=3)
            async def boot(_):
                await game.connect(bridge._server._server.sockets[0].getsockname()[1])
            try:
                report = await session.run(bootstrap=boot, startup_only=True)
                self.assertEqual(report.result, "STARTUP_READY")
                self.assertFalse(report.success)
                self.assertEqual(report.sent_actions, 0)
                self.assertFalse(game.s.paused)
                self.assertEqual(game.s.mode, "MANUAL")
            finally: await game.close()
