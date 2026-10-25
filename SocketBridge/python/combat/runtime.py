"""Single-writer execution, native pause coordination, audit log and runner."""
from __future__ import annotations
import asyncio
from collections import deque
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import time
from .models import ContextRef, RoomReport, ActionReceipt, Vec
from .catalog import compile_plan, default_plan
from .controller import BuildResolver, JointActionSolver
from .geometry import door_route
from .observation import SnapshotAssembler, CombatJudge
from .supervision import ProgressSupervisor


class RunLog:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=False)
        self.file = (directory/"trace.jsonl").open("w", encoding="utf-8")
        self.sequence = 0
        self.window = deque(maxlen=90)
        self.capture_until = -1
        self.last_trigger = {}

    def __call__(self, kind, **value):
        self.sequence += 1
        self.file.write(json.dumps({"sequence": self.sequence, "wall_monotonic": time.monotonic(),
                                    "kind": kind, **value}, ensure_ascii=False, allow_nan=False)+"\n")
        self.file.flush()

    def candidates(self, frame, candidates):
        row = {"frame": frame, "candidates": candidates}
        self.window.append(row)
        if frame <= self.capture_until: self("candidate_window", **row)

    def trigger(self, frame, reason):
        key = json.dumps(reason, sort_keys=True)
        if frame-self.last_trigger.get(key, -1000) < 90: return
        self.last_trigger[key] = frame
        self("debug_trigger", frame=frame, reason=reason, preceding_candidates=list(self.window))
        self.capture_until = frame+30

    def manifest(self, root, model):
        files = (list((root/"SocketBridge/python/combat").glob("*.py"))+
                 list((root/"SocketBridge/python/apps").glob("*.py"))+
                 [root/"SocketBridge/main.lua", root/"SocketBridge/python/facade.py",
                  root/"SocketBridge/python/connection/server.py", root/"run_first_room.py",
                  root/"run-first-room.ps1"])
        files = [p for p in files if p.is_file()]
        value = {"schema_version": 1, "platform": platform.platform(), "python": platform.python_version(),
                 "model": model, "game_expected": "Repentance 1.7.9b.J835", "normal_inputs_only": True,
                 "source_hashes": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
        try: value["git_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        except (OSError, subprocess.CalledProcessError): value["git_commit"] = None
        (self.directory/"manifest.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def finish(self, report):
        self("report", **report.model_dump())
        (self.directory/"report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
        self.file.close()


class NativePause:
    """Escape operates the game's own pause menu; never mutates game time.

    Only the two Escape presses need the game window in the OS foreground,
    because injected keys are delivered to the foreground window. The window is
    raised for that instant and the previous foreground window is restored right
    after, so the operator keeps the terminal. Movement and shooting never need
    focus at all: they travel through the mod's input cache while
    ``PauseOnFocusLost=0`` keeps the game simulating in the background.

    The activation check below is not a policy gate: it is the safety rule that
    keeps Escape from being injected into whatever else happens to be focused.
    """
    SW_RESTORE = 9

    def __init__(self, game_executable=None, game_pid=None):
        if os.name != "nt": raise RuntimeError("Native pause requires Windows")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        self.user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.IsWindow.argtypes = (wintypes.HWND,)
        self.user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        self.user32.IsIconic.argtypes = (wintypes.HWND,)
        self.user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        self.user32.BringWindowToTop.argtypes = (wintypes.HWND,)
        self.user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        self.user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self.window = None
        self.previous = None
        self.game_executable = Path(game_executable).resolve() if game_executable else None
        self.game_pid = game_pid

    def game_processes(self):
        """PIDs whose image name is the game executable.

        Title matching alone is not trustworthy: a File Explorer window open on
        the game folder is titled "The Binding of Isaac Rebirth Repentance" and
        would match a subtitle search, sending Escape to Explorer.
        """
        class Entry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                        ("pid", wintypes.DWORD), ("heap", ctypes.c_void_p),
                        ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                        ("parent", wintypes.DWORD), ("priority", ctypes.c_long),
                        ("flags", wintypes.DWORD), ("exe", wintypes.WCHAR*260)]
        self.kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        self.kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        self.kernel32.Process32FirstW.argtypes = (ctypes.c_void_p, ctypes.POINTER(Entry))
        self.kernel32.Process32NextW.argtypes = (ctypes.c_void_p, ctypes.POINTER(Entry))
        self.kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        self.kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel32.OpenProcess.restype = ctypes.c_void_p
        self.kernel32.QueryFullProcessImageNameW.argtypes = (
            ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        snapshot = self.kernel32.CreateToolhelp32Snapshot(2, 0)   # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == ctypes.c_void_p(-1).value: return set()
        found = set()
        try:
            entry = Entry(); entry.size = ctypes.sizeof(Entry)
            more = self.kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                if entry.exe.lower() == "isaac-ng.exe":
                    if self.game_pid is None or entry.pid == self.game_pid:
                        if self.game_executable is None:
                            found.add(entry.pid)
                        else:
                            process = self.kernel32.OpenProcess(0x1000, False, entry.pid)
                            if process:
                                try:
                                    path = ctypes.create_unicode_buffer(1024)
                                    size = wintypes.DWORD(len(path))
                                    if self.kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                                        if Path(path.value).resolve() == self.game_executable:
                                            found.add(entry.pid)
                                finally: self.kernel32.CloseHandle(process)
                more = self.kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            self.kernel32.CloseHandle(snapshot)
        return found

    def game_window(self, refresh=False):
        """Locate the window owned by the game process, never by title alone."""
        pids = self.game_processes()
        self.window = None
        if not pids: return None
        if len(pids) != 1: raise RuntimeError("MULTIPLE_GAME_PROCESSES")
        def visit(handle, _param):
            pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            if pid.value in pids and self.user32.IsWindowVisible(handle):
                self.window = handle; return False
            return True
        self.user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(visit), 0)
        return self.window

    def foreground(self): return self.user32.GetForegroundWindow()

    def focused(self): return bool(self.game_window()) and self.foreground() == self.game_window()

    def activate(self, handle):
        """Raise one window and report whether it actually took the foreground."""
        if not handle: return False
        if self.user32.IsIconic(handle): self.user32.ShowWindow(handle, self.SW_RESTORE)
        if self.foreground() == handle: return True
        target = self.user32.GetWindowThreadProcessId(handle, None)
        mine = self.kernel32.GetCurrentThreadId()
        foreground_thread = self.user32.GetWindowThreadProcessId(self.foreground(), None)
        attached = []
        for thread in dict.fromkeys((foreground_thread, target)):
            if thread and thread != mine and self.user32.AttachThreadInput(mine, thread, True):
                attached.append(thread)
        try:
            self.user32.BringWindowToTop(handle)
            self.user32.SetForegroundWindow(handle)
        finally:
            for thread in reversed(attached): self.user32.AttachThreadInput(mine, thread, False)
        return self.foreground() == handle

    def release_focus(self):
        """Best effort: hand the foreground back to whoever had it before."""
        previous, self.previous = self.previous, None
        if previous and self.user32.IsWindow(previous) and previous != self.foreground():
            self.activate(previous)

    async def toggle(self):
        await self.key(0x1B)

    async def key(self, vk):
        """Deliver a single key only to the verified game process."""
        handle = self.game_window() or self.game_window(refresh=True)
        if not handle: raise RuntimeError("GAME_WINDOW_NOT_FOUND")
        previous = self.foreground()
        if previous != handle: self.previous = previous
        try:
            deadline = time.monotonic()+.5
            while not self.activate(handle):
                if time.monotonic() > deadline: raise RuntimeError("GAME_WINDOW_ACTIVATION_FAILED")
                await asyncio.sleep(.03)
            await self.press_key(vk)
        finally:
            self.release_focus()

    async def press_escape(self):
        await self.press_key(0x1B)

    async def press_key(self, vk):
        # SendInput needs a full union-sized INPUT on both 32 and 64 bit Python.
        class Keyboard(ctypes.Structure):
            _fields_ = [("vk", wintypes.WORD), ("scan", wintypes.WORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]
        class Mouse(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("data", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]
        class Union(ctypes.Union): _fields_ = [("ki", Keyboard), ("mi", Mouse)]
        class Input(ctypes.Structure): _fields_ = [("type", wintypes.DWORD), ("u", Union)]
        self.user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int)
        if self.foreground() != self.game_window():
            raise RuntimeError("GAME_LOST_FOREGROUND_DURING_KEY")
        def send(flags):
            value = Input(type=1, u=Union(ki=Keyboard(vk=vk, flags=flags)))
            if self.user32.SendInput(1, ctypes.byref(value), ctypes.sizeof(Input)) != 1:
                raise RuntimeError("Native pause input failed")
        send(0)
        try:
            await asyncio.sleep(.06)
        finally:
            # Always release our own key, even on cancellation/focus loss. Never
            # leave a globally held key behind after a successful key-down.
            send(2)

    def request_close(self):
        handle = self.game_window(refresh=True)
        if not handle: raise RuntimeError("GAME_WINDOW_NOT_FOUND_FOR_CLOSE")
        self.user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        if not self.user32.PostMessageW(handle, 0x10, 0, 0):
            raise RuntimeError("GAME_CLOSE_REQUEST_FAILED")


class CombatSession:
    def __init__(self, bridge, planner, pause, log, *, ready_timeout=120, new_run=False, seed=None):
        self.bridge, self.planner, self.pause, self.log = bridge, planner, pause, log
        self.ready_timeout = ready_timeout
        self.new_run, self.seed = new_run, seed
        self.observation = SnapshotAssembler()
        self.changed = asyncio.Event()
        self.rpc_waiters = {}
        self.command_id = 0
        self.heartbeat = None
        self.heartbeat_at = 0.
        self.judge = CombatJudge()
        self.judge_result = None
        self.target_room = None
        self.transitions = 0
        self.actions = 0
        self.accepted = set()
        self.applied = set()
        self.rejected = None
        self.build_resolver = BuildResolver()
        self.solver = JointActionSolver()
        self.supervisor = ProgressSupervisor()
        self.previous_action = None
        self.replan_requests = 0
        self.clear_started = None
        self.clear_safe_since = None
        self.plan = None
        self.revision = 0
        self.pending = None
        self.last_replan = -1000
        self.event_reasons = set()
        self.llm_configured = False
        self.fallbacks = 0
        self.pause_seconds = 0.
        self.pause_owned = False
        self.timings = []
        self.visited = set()
        self.latest_acted = None
        self.started_at = time.monotonic()
        bridge.on_message(self.on_message)

    def on_message(self, raw):
        self.log("game_message", message=raw)
        request = raw.get("request_id")
        if request in self.rpc_waiters and not self.rpc_waiters[request].done():
            self.rpc_waiters[request].set_result(raw.get("result", {}))
        if raw.get("type") == "ACTION_RECEIPT":
            receipt = ActionReceipt.model_validate({k: raw[k] for k in ActionReceipt.model_fields})
            if receipt.accepted: self.accepted.add(receipt.action_id)
            else: self.rejected = receipt.model_dump()
        if raw.get("type") == "HEARTBEAT":
            self.heartbeat, self.heartbeat_at = raw, time.monotonic()
        s = self.observation.consume(raw)
        if s:
            if s.applied_action_id is not None: self.applied.add(s.applied_action_id)
            if s.room_visit_id == self.target_room and not s.paused:
                self.judge_result = self.judge.observe(s)
                self.event_reasons.update(self.judge.events)
                if self.judge.events: self.log.trigger(s.frame, self.judge.events)
            self.changed.set()

    async def rpc(self, command, params=None, timeout=2):
        self.command_id += 1
        request = f"phase1-{self.command_id}"
        future = asyncio.get_running_loop().create_future()
        self.rpc_waiters[request] = future
        try:
            sent = await self.bridge.send_command(command, params, request_id=request)
            if not sent: raise ConnectionError("Command could not be sent")
            try:
                result = await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError("COMMAND_TIMEOUT: "+command) from exc
            if result.get("success") is not True: raise RuntimeError(f"{command}: {result}")
            return result
        finally: self.rpc_waiters.pop(request, None)

    async def set_mode(self, mode):
        result = await self.rpc("SET_CONTROL_MODE", {"mode": mode})
        if result.get("mode") != mode: raise RuntimeError("Mode confirmation mismatch")

    async def next_snapshot(self, after=None, timeout=2):
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            s = self.observation.latest
            if s and s.snapshot_id != after and self.observation.fresh(): return s
            self.changed.clear()
            try: await asyncio.wait_for(self.changed.wait(), min(.1, max(.001, end-time.monotonic())))
            except asyncio.TimeoutError: pass
        raise TimeoutError("STALE_OBSERVATION: no fresh atomic combat snapshot")

    async def release(self):
        await self.bridge.send_input(move=(0, 0), shoot=(0, 0))

    async def send_action(self, s, move, shoot, metadata=None):
        if not self.bridge.connected: raise ConnectionError("DISCONNECTED")
        latest = self.observation.latest
        if not self.observation.fresh() or latest.room_visit_id != s.room_visit_id or latest.frame-s.frame > 2:
            await self.release(); raise TimeoutError("STALE_ACTION")
        if self.latest_acted == s.snapshot_id: raise RuntimeError("DUPLICATE_ACTION_FRAME")
        self.latest_acted = s.snapshot_id
        self.actions += 1
        sent = await self.bridge.send_input(move=move, shoot=shoot, action_id=self.actions,
            room_visit_id=s.room_visit_id, observed_frame=s.frame)
        self.log("action_sent", action_id=self.actions, sent=sent, frame=s.frame,
                 room_visit_id=s.room_visit_id, move=move, shoot=shoot, metadata=metadata)
        if not sent: raise ConnectionError("INPUT_SEND_FAILED")

    async def pause_for_plan(self, s):
        await self.release()
        if s.paused: raise RuntimeError("UNOWNED_PAUSE")
        began = time.monotonic()
        self.pause_owned = True
        await self.pause.toggle()
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            h = self.heartbeat
            if h and self.heartbeat_at > began and h["paused"] and h["room_visit_id"] == s.room_visit_id:
                # Require the game update count to remain frozen across heartbeats.
                f, stamp = h["frame"], self.heartbeat_at
                await asyncio.sleep(.22)
                if self.heartbeat_at > stamp and self.heartbeat["frame"] == f:
                    await self.rpc("GET_FULL_STATE")
                    frozen = self.observation.latest
                    if not frozen.paused or frozen.room_visit_id != s.room_visit_id: raise RuntimeError("PAUSE_CONTEXT_CHANGED")
                    self.log("pause_confirmed", entry_frame=s.frame, paused_frame=f, delay_ticks=f-s.frame)
                    return frozen, began
            await asyncio.sleep(.02)
        raise TimeoutError("PAUSE_NOT_CONFIRMED")

    def context_ref(self, s, build):
        return ContextRef(run_id=s.run_id, room_visit_id=s.room_visit_id, snapshot_id=s.snapshot_id,
                          build_revision=build.revision, plan_revision=self.revision)

    async def configure_room(self, s):
        frozen, began = s, None
        try:
            frozen, began = await self.pause_for_plan(s)
            build = self.build_resolver.update(frozen)
            if self.planner is None: raise ValueError("Local-only run requested")
            self.plan = await asyncio.wait_for(self.planner.plan(frozen, build, self.revision), 180)
            self.llm_configured = True
            self.log("plan_installed", source="deepseek", plan=self.plan.model_dump())
        except Exception as exc:
            self.fallbacks += 1
            self.plan = default_plan(self.context_ref(frozen, self.build_resolver.update(frozen)))
            self.log("fallback", error=type(exc).__name__+": "+str(exc), plan=self.plan.model_dump())
        finally:
            if self.pause_owned:
                # A transition/unknown pause state must not be blindly toggled.
                if self.heartbeat and time.monotonic()-self.heartbeat_at < 1 and self.heartbeat.get("paused"):
                    await self.pause.toggle()
                    self.pause.release_focus()
                else: raise RuntimeError("PAUSE_RESUME_UNCONFIRMED")
                self.pause_owned = False
            if began: self.pause_seconds += time.monotonic()-began
        fresh = await self.next_snapshot(after=frozen.snapshot_id)
        if fresh.room_visit_id != s.room_visit_id: raise RuntimeError("ROOM_CHANGED_DURING_PLAN")
        await self.set_mode("FORCE_AI")
        return await self.next_snapshot(after=fresh.snapshot_id)

    async def navigate(self, s):
        initial_run = s.run_id
        if not s.room.get("is_starting_room") or s.room["stage"] != 1 or s.room["difficulty"] != 0 or s.hostiles:
            raise RuntimeError("START_CONTRACT: begin a fresh Isaac Normal run in its starting room")
        for _ in range(8):
            self.build_resolver.update(s)
            self.visited.add(s.room["room_idx"])
            route = door_route(s, self.visited)
            if route is None: raise RuntimeError("NO_REACHABLE_ORDINARY_DOOR")
            _, slot, door, path = route
            self.log("door_selected", slot=slot, target_room=door["target_room"], path=[p.model_dump() for p in path])
            await self.set_mode("FORCE_AI")
            room, deadline = s.room_visit_id, time.monotonic()+25
            goal = Vec(x=door["x"], y=door["y"])
            while s.room_visit_id == room:
                if time.monotonic() > deadline: raise TimeoutError("DOOR_NAVIGATION_TIMEOUT")
                if s.run_id != initial_run: raise RuntimeError("RUN_CHANGED")
                while path and s.player.pos.distance(path[0]) < 22: path.pop(0)
                point = path[0] if path else goal
                delta = point-s.player.pos
                # Continue beyond the door center so that game collision triggers
                # the transition, including when a frame lands exactly at center.
                if not path and delta.length() < 20:
                    direction = slot % 4
                    move = ((-1, 0), (0, -1), (1, 0), (0, 1))[direction]
                else:
                    move = (0 if abs(delta.x) < 7 else (1 if delta.x > 0 else -1),
                            0 if abs(delta.y) < 7 else (1 if delta.y > 0 else -1))
                await self.send_action(s, move, (0, 0), {"state": "EXIT_START"})
                prior = s
                s = await self.next_snapshot(after=s.snapshot_id)
                if s.player.dead: raise RuntimeError("DEAD_DURING_NAVIGATION")
            await self.release()
            self.transitions += 1
            # Account for damage on entry and during the enemy-spawn grace
            # period, before planning begins. Empty rooms get a new judge too.
            self.target_room = s.room_visit_id
            self.judge = CombatJudge()
            self.judge.previous = prior
            self.judge_result = self.judge.observe(s)
            # Transitional snapshots can precede enemy creation. Observe a few
            # updates before classifying a newly entered room as empty.
            for _ in range(3): s = await self.next_snapshot(after=s.snapshot_id)
            if s.hostiles or not s.room["is_clear"]: return s
        raise RuntimeError("NO_COMBAT_ROOM_FOUND")

    async def engage(self, s):
        self.target_room = s.room_visit_id
        s = await self.configure_room(s)
        while True:
            if s.room_visit_id != self.target_room: raise RuntimeError("UNEXPECTED_ROOM_CHANGE")
            if s.mode == "MANUAL": raise RuntimeError("MANUAL_TAKEOVER")
            if self.rejected:
                self.log("rejected_action", receipt=self.rejected); self.rejected = None
                await self.release()
            if self.judge_result and self.judge_result != "CLEARED": return self.judge_result
            build = self.build_resolver.update(s)
            failure = self.supervisor.observe(s, self.previous_action)
            for event in self.supervisor.events:
                self.log("boundary", boundary=event.__dict__, evidence=self.supervisor.evidence)
                if event.age_ticks >= 60 or event.kind in ("no_safe_candidate", "escape_margin"):
                    self.event_reasons.add(event.kind)
            if failure: return failure
            self.solver.recovery_level = self.supervisor.recovery_level
            if self.pending and self.pending.done():
                try:
                    value = self.pending.result()
                    self.plan = compile_plan(value, self.context_ref(s, build))
                    self.revision += 1
                    self.solver.profile_name = None
                    self.log("plan_installed", source="deepseek_replan", plan=self.plan.model_dump())
                except Exception as exc:
                    self.log("replan_rejected", error=type(exc).__name__+": "+str(exc))
                self.pending = None
            recent = [f for f in self.solver.recoveries if s.frame-f <= 150]
            if len(recent) >= 2: self.event_reasons.add("repeated_recovery")
            # Mandatory supervision is not policy-controlled by replan_on.
            reasons = sorted(self.event_reasons)
            severe = bool(set(reasons) & {"idle", "blocked", "stall", "no_safe_candidate", "escape_margin"})
            budget = 6 if severe else 4
            if reasons and self.planner and not self.pending and self.replan_requests < budget and (severe or s.frame-self.last_replan >= 90):
                self.last_replan = s.frame; self.event_reasons.clear()
                self.replan_requests += 1
                feedback = {"reasons": reasons, "damage_half_hearts": self.judge.damage, "current_plan": self.plan.model_dump(),
                            "progress": self.supervisor.metrics(), "last_action": self.previous_action.model_dump() if self.previous_action else None}
                self.pending = asyncio.create_task(asyncio.wait_for(self.planner.plan(s, build.model_copy(deep=True), self.revision, feedback), 20))
                self.log("replan_started", frame=s.frame, reasons=reasons)
            action = await asyncio.to_thread(self.solver.solve, s, self.plan, build.model_copy(deep=True))
            if self.judge_result == "CLEARED":
                if self.clear_started is None: self.clear_started = s.frame
                if not action.diagnostics["no_safe_candidate"] and action.score["escape_slack"] >= 6:
                    if self.clear_safe_since is None: self.clear_safe_since = s.frame
                    if s.frame-self.clear_safe_since >= 15:
                        self.log("safe_clear_confirmed", frame=s.frame, started=self.clear_started, safe_since=self.clear_safe_since)
                        return "CLEARED"
                else: self.clear_safe_since = None
                if s.frame-self.clear_started >= 90: return "CLEAR_UNSAFE"
            else:
                self.clear_started = self.clear_safe_since = None
            self.previous_action = action
            self.timings.append(action.decision_ms)
            self.log.candidates(s.frame, self.solver.last_candidates)
            await self.send_action(s, action.move, action.shoot, action.model_dump())
            s = await self.next_snapshot(after=s.snapshot_id)

    async def run(self, bootstrap=None, startup_only=False, on_ready=None):
        result, error = "ABORTED", None
        try:
            await self.bridge.start()
            if bootstrap: await bootstrap(self)
            deadline = time.monotonic()+self.ready_timeout
            while not self.bridge.connected:
                if time.monotonic() > deadline: raise TimeoutError("GAME_CONNECTION_TIMEOUT")
                await asyncio.sleep(.05)
            await self.bridge.subscribe(["COMBAT_FRAME"], {"COMBAT_FRAME": {"enabled": True}})
            await self.rpc("GET_FULL_STATE", timeout=10)
            initial = await self.next_snapshot()
            if self.new_run or self.seed:
                # Setup commands are deliberately restricted and run before the
                # measured task. No console authority is exposed to the planner.
                old_run = initial.run_id
                self.log("initialization_command", command="PREPARE_RUN", seed=self.seed, character=0)
                await self.rpc("PREPARE_RUN", {"seed": self.seed or ""})
                deadline = time.monotonic()+10
                while initial.run_id == old_run:
                    if time.monotonic() > deadline: raise TimeoutError("NEW_RUN_NOT_CONFIRMED")
                    initial = await self.next_snapshot(after=initial.snapshot_id)
                for _ in range(3): initial = await self.next_snapshot(after=initial.snapshot_id)
                if self.seed and initial.seed != self.seed: raise RuntimeError("SEED_NOT_CONFIRMED")
            self.log("run_start", seed=initial.seed, snapshot_id=initial.snapshot_id)
            if on_ready: await on_ready(self)
            if startup_only:
                if not initial.room.get("is_starting_room") or initial.room["stage"] != 1 or initial.room["difficulty"] != 0:
                    raise RuntimeError("START_CONTRACT")
                self.build_resolver.update(initial)
                frozen, began = await self.pause_for_plan(initial)
                await self.pause.toggle()
                self.pause_owned = False
                self.pause_seconds += time.monotonic()-began
                await self.next_snapshot(after=frozen.snapshot_id)
                result = "STARTUP_READY"
            else:
                target = await self.navigate(initial)
                result = await self.engage(target)
        except asyncio.CancelledError:
            error = "INTERRUPTED"
        except Exception as exc:
            error = type(exc).__name__+": "+str(exc)
            self.log("fault", error=error)
        finally:
            if self.pending:
                self.pending.cancel(); await asyncio.gather(self.pending, return_exceptions=True)
            if self.bridge.connected:
                if self.pause_owned and self.heartbeat and time.monotonic()-self.heartbeat_at < 1 and self.heartbeat.get("paused"):
                    try:
                        await self.pause.toggle()
                        self.pause_owned = False
                    except Exception as exc: self.log("cleanup_error", error="resume: "+str(exc))
                try:
                    await self.release(); await self.set_mode("MANUAL")
                except Exception as exc: self.log("cleanup_error", error=str(exc))
            await self.bridge.stop()
        s = self.observation.latest
        report = RoomReport(result=result, success=result == "CLEARED" and self.transitions > 0,
            llm_configured=self.llm_configured, completed_door_transition=self.transitions > 0,
            damage_half_hearts=self.judge.damage, lost_bone_hearts=self.judge.bones_lost,
            remaining_half_hearts=s.hp if s else None,
            combat_ticks=max(0, (self.judge.previous.frame-self.judge.started)) if self.judge.previous else 0,
            wall_seconds=time.monotonic()-self.started_at, pause_seconds=self.pause_seconds,
            sent_actions=self.actions, accepted_actions=len(self.accepted), observed_applied_actions=len(self.applied),
            fallback_count=self.fallbacks, recovery_count=len(self.solver.recoveries),
            decision_p95_ms=sorted(self.timings)[min(len(self.timings)-1, int(.95*len(self.timings)))] if self.timings else None,
            seed=s.seed if s else None, error=error, progress_metrics=self.supervisor.metrics(),
            replan_requests=self.replan_requests,
            decision_p99_ms=sorted(self.timings)[min(len(self.timings)-1, int(.99*len(self.timings)))] if self.timings else None,
            safe_clear_ticks=max(0, s.frame-self.clear_started) if self.clear_started is not None and s else 0)
        self.log.finish(report)
        return report
