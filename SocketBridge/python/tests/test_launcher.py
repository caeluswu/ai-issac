import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from combat.launcher import GameLauncher, OPTIONS, patch_options, sha256
from combat.runtime import NativePause, RunLog
from connection.server import BridgeServer


class TestLauncherFiles(unittest.TestCase):
    def test_save_baseline_integrity_slot_and_path_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/"baseline.dat"; source.write_bytes(b"owner supplied save")
            launcher = GameLauncher(root, Mock(), Mock())
            launcher.baseline = {"files": {"persistentgamedata1.dat": {"path": "baseline.dat", "sha256": sha256(source)}}}
            self.assertEqual(launcher.baseline_files()[0][1], source)
            source.write_bytes(b"modified")
            with self.assertRaisesRegex(ValueError, "HASH_MISMATCH"): launcher.baseline_files()
            launcher.baseline["files"]["persistentgamedata1.dat"]["path"] = "../outside.dat"
            with self.assertRaisesRegex(ValueError, "OUTSIDE_PROJECT"): launcher.baseline_files()

    def test_activation_attaches_foreground_and_target_threads_then_detaches(self):
        pause = object.__new__(NativePause)
        pause.user32 = Mock()
        pause.kernel32 = Mock()
        pause.user32.IsIconic.return_value = False
        pause.kernel32.GetCurrentThreadId.return_value = 30
        pause.user32.GetWindowThreadProcessId.side_effect = lambda hwnd, _: {1: 10, 2: 20}[hwnd]
        pause.foreground = Mock(side_effect=[1, 1, 2])
        self.assertTrue(pause.activate(2))
        calls = [c.args for c in pause.user32.AttachThreadInput.call_args_list]
        self.assertEqual(calls, [(30, 10, True), (30, 20, True), (30, 20, False), (30, 10, False)])

    def test_preserves_unrelated_options_and_crlf(self):
        original = b"[Options]\r\nCustom=keep=this\r\n"+b"".join(
            (key+"=9\r\n").encode() for key in OPTIONS)
        result = patch_options(original)
        self.assertIn(b"Custom=keep=this\r\n", result)
        self.assertEqual(result.count(b"\r\n"), original.count(b"\r\n"))
        for key, value in OPTIONS.items(): self.assertIn(f"{key}={value}\r\n".encode(), result)
        self.assertEqual(patch_options(result), result)

    def test_missing_option_fails_without_partial_write(self):
        with self.assertRaisesRegex(ValueError, "MISSING_GAME_OPTION"):
            patch_options(b"Fullscreen=1\n")

    def test_prepare_preserves_save_and_backs_up_installed_mod(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save = root/"save"; save.mkdir()
            options = b"".join((key+"=9\n").encode() for key in OPTIONS)
            (save/"options.ini").write_bytes(options)
            (save/"rep_persistentgamedata1.dat").write_bytes(b"user save")
            (save/"log.txt").write_text("old evidence")
            source = root/"SocketBridge"; source.mkdir()
            for name in ("main.lua", "metadata.xml"): (source/name).write_text("new "+name)
            log = RunLog(root/"run")
            launcher = GameLauncher(root, log, Mock())
            launcher.save_dir = save
            baseline = root/"baseline"; baseline.write_bytes(b"frozen unlocked save")
            launcher.baseline = {"unlock_status": "owner_declared_fully_unlocked", "files": {
                "persistentgamedata1.dat": {"path": "baseline", "sha256": sha256(baseline)}}}
            destination = launcher.game_dir/"mods/SocketBridge"; destination.mkdir(parents=True)
            (destination/"main.lua").write_text("old mod")
            (destination/"disable.it").touch()
            try:
                launcher.prepare()
                self.assertEqual((save/"rep_persistentgamedata1.dat").read_bytes(), b"user save")
                self.assertEqual((save/"persistentgamedata1.dat").read_bytes(), b"frozen unlocked save")
                self.assertEqual((log.directory/"backup/rep_persistentgamedata1.dat").read_bytes(), b"user save")
                self.assertEqual((log.directory/"backup/options.ini").read_bytes(), options)
                self.assertEqual((log.directory/"backup/mod-main.lua").read_text(), "old mod")
                self.assertEqual(sha256(destination/"main.lua"), sha256(source/"main.lua"))
                self.assertFalse((destination/"disable.it").exists())
                self.assertEqual((save/"log.txt").read_bytes(), b"")
            finally: log.file.close()


class TestLauncherAsync(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_games_not_closed_or_launched(self):
        pause = Mock(); pause.game_processes.return_value = {1, 2}
        launcher = GameLauncher(Path("."), Mock(), pause)
        with self.assertRaisesRegex(RuntimeError, "MULTIPLE_GAME_PROCESSES"):
            await launcher.close_existing()
        pause.request_close.assert_not_called()

    async def test_focus_restored_when_key_delivery_fails(self):
        pause = object.__new__(NativePause)
        pause.previous = None
        pause.game_window = Mock(return_value=20)
        pause.foreground = Mock(return_value=10)
        pause.activate = Mock(return_value=True)
        pause.press_key = AsyncMock(side_effect=RuntimeError("injection failed"))
        pause.release_focus = Mock()
        with self.assertRaisesRegex(RuntimeError, "injection failed"): await pause.key(13)
        pause.release_focus.assert_called_once()

    async def test_bootstrap_failure_restores_focus_and_records_error(self):
        with tempfile.TemporaryDirectory() as directory:
            log = RunLog(Path(directory)/"run")
            pause = Mock(); pause.foreground.return_value = 77
            launcher = GameLauncher(Path(directory), log, pause)
            try:
                with self.assertRaises(FileNotFoundError): await launcher(Mock())
                pause.activate.assert_called_once_with(77)
                self.assertEqual(json.loads((log.directory/"startup.json").read_text())[-1]["phase"], "failed")
            finally: log.file.close()

    async def test_replacement_connection_retains_new_writer(self):
        server = BridgeServer(port=0)
        received = []
        async def message(value): received.append(value)
        server.on_message(message)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        try:
            await asyncio.wait_for(r1.read(), 2)
            w2.write(b'{"test":2}\n'); await w2.drain()
            for _ in range(50):
                if received: break
                await asyncio.sleep(.01)
            self.assertEqual(received, [{"test": 2}])
            self.assertTrue(server.connected)
            self.assertTrue(await server.send({"alive": True}))
            self.assertEqual(json.loads(await asyncio.wait_for(r2.readline(), 2)), {"alive": True})
        finally:
            w1.close(); w2.close()
            await w1.wait_closed(); await w2.wait_closed()
            await server.stop()
