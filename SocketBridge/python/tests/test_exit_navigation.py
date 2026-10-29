import json
from pathlib import Path
import unittest
from campaign.exit_navigation import ExitNavigator
from combat.models import WorldSnapshot


class TestExitNavigation(unittest.TestCase):
    def source(self):
        return WorldSnapshot.model_validate(json.loads((Path(__file__).parent/'fixtures/floor5_exit_stall.json').read_text(encoding='utf8')))

    def test_closed_exit_requires_retreat_then_observed_open(self):
        s=self.source();exit=s.exit_observations[0];exit.animation='Closed'
        nav=ExitNavigator(exit);goal=nav.step(s)
        self.assertEqual(nav.phase,'withdraw')
        self.assertGreaterEqual(goal.distance(exit.pos),90)
        s.player.pos=goal;s.frame+=30
        nav.step(s);self.assertEqual(nav.phase,'wait_open')
        exit.state=1;exit.animation='Open Animation';s.frame+=1
        self.assertEqual(nav.step(s),goal)
        exit.animation='Opened';s.frame+=15
        self.assertEqual(nav.step(s),exit.pos)
        self.assertEqual(nav.phase,'approach')

    def test_wait_and_identity_are_bounded(self):
        s=self.source();nav=ExitNavigator(s.exit_observations[0]);s.player.pos=nav.step(s)
        nav.step(s);s.frame+=61
        with self.assertRaisesRegex(TimeoutError,'EXIT_DID_NOT_OPEN'):nav.step(s)
        s.exit_observations=[]
        with self.assertRaisesRegex(RuntimeError,'EXIT_TARGET_CHANGED'):nav.step(s)

    def test_legacy_state_is_supported_but_closed_animation_is_not_overridden(self):
        s=self.source();exit=s.exit_observations[0];exit.state=1
        self.assertEqual(ExitNavigator(exit).step(s),exit.pos)
        exit.animation='Closed'
        nav=ExitNavigator(exit);self.assertNotEqual(nav.step(s),exit.pos)
