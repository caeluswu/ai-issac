"""Fresh menu acknowledgements, rather than startup's preload log, prove a slot."""
import itertools
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from combat.launcher import GameLauncher


BOOT = ('Repentance v1.7.9b.J835\nMenu Manager Init\n'
        'Loading GameState 1 from local folder!\nLoading GameState 2 from local folder!\n'
        'Loading GameState 3 from local folder!\nMenu Key Config Init\n')


class TestMenuHandshake(unittest.IsolatedAsyncioTestCase):
    def launcher(self):
        pause = Mock(); pause.key = AsyncMock(); pause.game_window.return_value = 1
        launcher = GameLauncher(Path('.'), Mock(), pause)
        launcher.record = Mock()
        launcher.process = Mock(); launcher.process.poll.return_value = None
        launcher.text = BOOT
        launcher.read_game_log = lambda: launcher.text
        return launcher

    async def test_missing_confirm_is_retried_before_measurement(self):
        launcher = self.launcher()
        async def key(_):
            if launcher.pause.key.await_count == 2: launcher.text += 'Loading GameState 1 from local folder!\n'
        launcher.pause.key.side_effect = key
        with patch('combat.launcher.time') as clock, patch('combat.launcher.asyncio.sleep', new=AsyncMock()):
            clock.monotonic.side_effect = itertools.count(0, .5)
            self.assertEqual(await launcher.confirm_save_slot(3), 2)
        self.assertEqual(launcher.pause.key.await_count, 2)

    async def test_old_preload_does_not_confirm_and_retries_are_bounded(self):
        launcher = self.launcher(); launcher.text = 'Loading GameState 1 from local folder!\n'
        with patch('combat.launcher.time') as clock, patch('combat.launcher.asyncio.sleep', new=AsyncMock()):
            clock.monotonic.side_effect = itertools.count(0, .5)
            with self.assertRaisesRegex(RuntimeError, 'SAVE_SLOT_NOT_CONFIRMED'):
                await launcher.confirm_save_slot(1)
        self.assertEqual(launcher.pause.key.await_count, 3)

    async def test_fresh_wrong_slot_fails_without_continuing(self):
        launcher = self.launcher()
        async def key(_): launcher.text += 'Loading GameState 2 from local folder!\n'
        launcher.pause.key.side_effect = key
        with self.assertRaisesRegex(RuntimeError, 'SAVE_SLOT_MISMATCH'):
            await launcher.confirm_save_slot(3)
        self.assertEqual(launcher.pause.key.await_count, 1)

    async def test_delayed_log_is_polled_without_an_extra_confirm(self):
        launcher = self.launcher(); reads = 0
        def read():
            nonlocal reads
            reads += 1
            return launcher.text+('Loading GameState 1 from local folder!\n' if reads >= 4 else '')
        launcher.read_game_log = read
        with patch('combat.launcher.time') as clock, patch('combat.launcher.asyncio.sleep', new=AsyncMock()):
            clock.monotonic.side_effect = itertools.count(0, .2)
            self.assertEqual(await launcher.confirm_save_slot(3), 1)
        self.assertEqual(launcher.pause.key.await_count, 1)

    async def test_menu_boot_stops_enter_keys_as_soon_as_bridge_connects(self):
        launcher = self.launcher(); session = Mock(); session.bridge.connected = False
        async def key(_):
            n = launcher.pause.key.await_count
            if n == 2: launcher.text += 'Loading GameState 1 from local folder!\n'
            if n == 3: session.bridge.connected = True
        launcher.pause.key.side_effect = key
        with patch('combat.launcher.asyncio.sleep', new=AsyncMock()):
            await launcher.boot_menus(session)
        self.assertEqual(launcher.pause.key.await_count, 3)

    async def test_lost_intro_key_retries_until_menu_ack(self):
        launcher = self.launcher(); launcher.text = 'playing cutscene 1 (Intro).\n'
        async def key(_):
            if launcher.pause.key.await_count == 2: launcher.text += BOOT
        launcher.pause.key.side_effect = key
        with patch('combat.launcher.time') as clock, patch('combat.launcher.asyncio.sleep', new=AsyncMock()):
            clock.monotonic.side_effect = itertools.count(0, .5)
            self.assertEqual(await launcher.skip_intro(), 2)
        self.assertEqual(launcher.pause.key.await_count, 2)

    async def test_intro_retries_are_bounded_without_menu_ack(self):
        launcher = self.launcher(); launcher.text = 'playing cutscene 1 (Intro).\n'
        with patch('combat.launcher.time') as clock, patch('combat.launcher.asyncio.sleep', new=AsyncMock()):
            clock.monotonic.side_effect = itertools.count(0, .5)
            with self.assertRaisesRegex(RuntimeError, 'INTRO_SKIP_NOT_CONFIRMED'): await launcher.skip_intro()
        self.assertEqual(launcher.pause.key.await_count, 3)

    async def test_initialized_menu_does_not_receive_an_intro_key(self):
        launcher = self.launcher()
        self.assertEqual(await launcher.skip_intro(), 0)
        launcher.pause.key.assert_not_awaited()

