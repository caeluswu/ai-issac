"""Reproducible Windows bootstrap for the pinned, original Repentance build."""
from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

EXE_SHA256 = "bb9fda2535d94ae72e7fd4cd7d4a87dc9bfc27417e86f8f9190bfeeb8c9f10c2"
OPTIONS = {"Fullscreen": 0, "PauseOnFocusLost": 0, "EnableMods": 1,
           "EnableDebugConsole": 1, "VSync": 1, "MusicEnabled": 0}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def patch_options(data: bytes, values=OPTIONS):
    """Keep unrelated settings, encoding, comments and existing line endings."""
    for key, value in values.items():
        pattern = rb"(?m)^("+re.escape(key.encode())+rb"\s*=\s*)[^\r\n]*"
        if not re.search(pattern, data):
            raise ValueError("MISSING_GAME_OPTION: "+key)
        data = re.sub(pattern, lambda m: m[1]+str(value).encode(), data)
    return data


class GameLauncher:
    def __init__(self, root: Path, log, pause, slot=1, timeout=75, require_baseline=False):
        self.root, self.log, self.pause = root, log, pause
        self.game_dir = root/"The Binding of Isaac Rebirth Repentance"
        self.exe = self.game_dir/"isaac-ng.exe"
        self.slot, self.timeout = slot, timeout
        self.process = None
        self.save_dir = None
        self.records = []
        self.original_foreground = None
        self.require_baseline = require_baseline
        self.baseline = None

    def baseline_files(self):
        if not self.baseline: return []
        files = []
        for name, value in self.baseline["files"].items():
            if name not in (f"persistentgamedata{self.slot}.dat", f"gamestate{self.slot}.dat"):
                raise ValueError("INVALID_BASELINE_FILE")
            path = (self.root/value["path"]).resolve()
            if not path.is_relative_to(self.root.resolve()): raise ValueError("BASELINE_OUTSIDE_PROJECT")
            if sha256(path) != value["sha256"]: raise ValueError("SAVE_BASELINE_HASH_MISMATCH")
            files.append((name, path, value["sha256"]))
        if f"persistentgamedata{self.slot}.dat" not in self.baseline["files"]:
            raise ValueError("BASELINE_HAS_NO_PERSISTENT_SAVE")
        return files

    def record(self, phase, **data):
        row = {"phase": phase, "time": time.time(), **data}
        self.records.append(row)
        (self.log.directory/"startup.json").write_text(
            json.dumps(self.records, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log("startup", **row)
        print("[startup] "+phase, flush=True)

    def preflight(self):
        if self.slot not in (1, 2, 3): raise ValueError("INVALID_SAVE_SLOT")
        if not self.exe.is_file(): raise FileNotFoundError(self.exe)
        digest = sha256(self.exe)
        if digest != EXE_SHA256: raise ValueError("UNSUPPORTED_GAME_EXECUTABLE: "+digest)
        text = (self.game_dir/"savedatapath.txt").read_text(encoding="utf-8-sig")
        match = re.search(r"(?m)^Save Data Path:\s*(.+)$", text)
        if not match: raise ValueError("SAVE_DIRECTORY_NOT_KNOWN")
        self.save_dir = Path(match[1].strip()).resolve()
        if not (self.save_dir/"options.ini").is_file(): raise FileNotFoundError(self.save_dir/"options.ini")
        # Validate the complete options file before changing any file.
        patch_options((self.save_dir/"options.ini").read_bytes())
        profile = self.root/"configs/task"/f"save-baseline.slot-{self.slot}.json"
        if profile.exists():
            self.baseline = json.loads(profile.read_text(encoding="utf-8"))
            if self.baseline["slot"] != self.slot: raise ValueError("BASELINE_SLOT_MISMATCH")
            self.baseline_files()
        elif self.require_baseline: raise FileNotFoundError("SAVE_BASELINE_MISSING: "+str(profile))
        others = [p.name for p in (self.game_dir/"mods").iterdir()
                  if p.is_dir() and p.name.lower() != "socketbridge" and not (p/"disable.it").exists()]
        if others: raise ValueError("OTHER_ENABLED_MODS: "+", ".join(others))
        for name in ("main.lua", "metadata.xml"):
            if not (self.root/"SocketBridge"/name).is_file(): raise FileNotFoundError(name)
        self.record("preflight", executable_sha256=digest, slot=self.slot,
                    save_directory=str(self.save_dir), options=OPTIONS)

    async def close_existing(self):
        pids = self.pause.game_processes()
        if len(pids) > 1: raise RuntimeError("MULTIPLE_GAME_PROCESSES: close duplicate instances")
        if not pids: return
        self.record("closing_existing_game", pids=sorted(pids))
        self.pause.request_close()
        deadline = time.monotonic()+15
        while self.pause.game_processes():
            if time.monotonic() >= deadline: raise TimeoutError("GAME_DID_NOT_CLOSE: no forced termination")
            await asyncio.sleep(.1)

    def prepare(self):
        backup = self.log.directory/"backup"
        backup.mkdir()
        for path in self.save_dir.glob("*.dat"):
            shutil.copy2(path, backup/path.name)
        restored = {}
        for name, path, expected in self.baseline_files():
            shutil.copyfile(path, self.save_dir/name)
            actual = sha256(self.save_dir/name)
            if actual != expected: raise RuntimeError("SAVE_BASELINE_RESTORE_FAILED")
            restored[name] = actual
        options = self.save_dir/"options.ini"
        shutil.copy2(options, backup/"options.ini")
        options.write_bytes(patch_options(options.read_bytes()))
        destination = self.game_dir/"mods/SocketBridge"
        destination.mkdir(parents=True, exist_ok=True)
        if (destination/"disable.it").exists():
            shutil.copy2(destination/"disable.it", backup/"disable.it")
            (destination/"disable.it").unlink()
        for name in ("main.lua", "metadata.xml"):
            path = destination/name
            if path.exists(): shutil.copy2(path, backup/("mod-"+name))
            shutil.copy2(self.root/"SocketBridge"/name, path)
        self.record("prepared", installed_mod_sha256=sha256(destination/"main.lua"),
                    saves_before={p.name: sha256(p) for p in backup.glob("*.dat")},
                    restored_baseline=restored, unlock_status=self.baseline.get("unlock_status") if self.baseline else "unverified")
        game_log = self.save_dir/"log.txt"
        # The game truncates log.txt at boot. Remove stale evidence only after
        # preserving it, with the game confirmed stopped.
        if game_log.exists():
            shutil.copy2(game_log, backup/"game-log.txt")
            game_log.write_bytes(b"")

    def read_game_log(self):
        path = self.save_dir/"log.txt"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    async def wait_for(self, predicate, reason, timeout=None):
        deadline = time.monotonic()+(timeout or self.timeout)
        while not predicate():
            if self.process and self.process.poll() is not None:
                raise RuntimeError(f"GAME_EXITED: {self.process.returncode}, phase={reason}")
            if time.monotonic() >= deadline: raise TimeoutError(reason)
            await asyncio.sleep(.1)

    async def confirm_save_slot(self, previous_loads):
        """Require a new slot load, never startup's preload of all three slots.

        On slot one, Enter can safely advance either title or save selection;
        retry a missed transition before any measured run starts. Other slots
        cannot replay arrows without knowing the current menu, so fail closed.
        """
        def loads(): return re.findall(r"Loading GameState (\d) from", self.read_game_log())
        def confirmed():
            current = loads()
            if len(current) <= previous_loads: return False
            if int(current[-1]) != self.slot:
                raise RuntimeError(f"SAVE_SLOT_MISMATCH: expected={self.slot}, observed={current[-1]}")
            return True
        attempts = 3 if self.slot == 1 else 1
        for attempt in range(1, attempts+1):
            if confirmed(): return attempt-1
            self.record("save_slot_confirm_attempt", slot=self.slot, attempt=attempt)
            await self.pause.key(0x0D)
            try:
                await self.wait_for(confirmed, "SAVE_SLOT_CONFIRM_WAIT", timeout=2)
                return attempt
            except TimeoutError:
                continue
        raise RuntimeError("SAVE_SLOT_NOT_CONFIRMED")

    async def boot_menus(self, session):
        await self.wait_for(lambda: self.pause.game_window(refresh=True), "GAME_WINDOW_TIMEOUT")
        await self.wait_for(lambda: "playing cutscene 1" in self.read_game_log() or
                            "Menu Manager Init" in self.read_game_log(), "GAME_BOOT_TIMEOUT")
        if "Menu Manager Init" not in self.read_game_log():
            await self.pause.key(0x0D)  # skip intro
        await self.wait_for(lambda: "Menu Key Config Init" in self.read_game_log(), "MENU_INIT_TIMEOUT")
        if "Repentance v1.7.9b.J835" not in self.read_game_log(): raise RuntimeError("GAME_BUILD_LOG_MISMATCH")
        # Pinned vanilla build has no menu API. These bounded key steps are only
        # bootstrap; a fresh typed in-game snapshot is the success criterion.
        self.record("menus", slot=self.slot)
        previous_loads = len(re.findall(r"Loading GameState (\d) from", self.read_game_log()))
        await asyncio.sleep(1)
        await self.pause.key(0x0D)  # title -> save slots
        await asyncio.sleep(1)
        # Fresh process starts on slot 1; left/right wrap rather than clamp.
        for key in [0x27]*(self.slot-1):
            await self.pause.key(key)
            await asyncio.sleep(.2)
        attempts = await self.confirm_save_slot(previous_loads)
        self.record("save_slot_confirmed", slot=self.slot, attempts=attempts)
        # A restored save may offer Continue, whereas a fresh slot offers New
        # Run. Stop menu input as soon as the in-game bridge actually connects.
        for attempt in range(1, 5):
            if session.bridge.connected: break
            await self.pause.key(0x0D)
            try:
                await self.wait_for(lambda: session.bridge.connected, "GAME_ENTRY_WAIT", timeout=3)
                break
            except TimeoutError:
                self.record("game_entry_wait", attempt=attempt)
        await self.wait_for(lambda: session.bridge.connected, "GAME_CONNECTION_TIMEOUT")
        self.record("bridge_connected", pid=self.process.pid)

    async def __call__(self, session):
        self.original_foreground = self.pause.foreground()
        try:
            self.preflight()
            await self.close_existing()
            self.prepare()
            self.process = subprocess.Popen([str(self.exe), "--luadebug"], cwd=self.game_dir)
            self.pause.game_pid = self.process.pid
            self.record("launched", pid=self.process.pid, cwd=str(self.game_dir), args=["--luadebug"])
            await self.boot_menus(session)
        except BaseException as exc:
            self.record("failed", error=type(exc).__name__+": "+str(exc))
            raise
        finally:
            if self.original_foreground: self.pause.activate(self.original_foreground)

    def collect_evidence(self):
        if not self.save_dir: return
        source = self.save_dir/"log.txt"
        if source.exists(): shutil.copy2(source, self.log.directory/"game-log.txt")
        (self.log.directory/"saves-after.json").write_text(json.dumps(
            {p.name: sha256(p) for p in self.save_dir.glob("*.dat")}, indent=2), encoding="utf-8")
