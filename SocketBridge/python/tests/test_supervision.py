import unittest
from combat.models import ActionFrame
from combat.supervision import ProgressSupervisor
from tests.combat_fixtures import snapshot


def action(move=(0, 0), shoot=(0, 0), **diagnostics):
    return ActionFrame(frame=0, room_visit_id="test:1", move=move, shoot=shoot,
                       profile="safe", target="e1", state="ENGAGE", score={}, decision_ms=0,
                       diagnostics=diagnostics)


class TestProgress(unittest.TestCase):
    def test_zero_input_escalates_and_fails_before_global_timeout(self):
        monitor = ProgressSupervisor(); events = []
        for frame in range(241):
            result = monitor.observe(snapshot(frame=frame), action())
            events.extend(monitor.events)
        self.assertTrue(any(e.kind == "idle" and e.frame == 30 for e in events))
        self.assertTrue(any(e.kind == "stall" and e.frame == 60 for e in events))
        self.assertEqual(result, "STALL_UNRECOVERED")

    def test_continuous_damage_allows_stationary_attack(self):
        monitor = ProgressSupervisor()
        for frame in range(400):
            s = snapshot(frame=frame); s.enemies[0].hp = 1000-frame*.1
            self.assertIsNone(monitor.observe(s, action(shoot=(1, 0))))
        self.assertEqual(monitor.recovery_level, 0)

    def test_entity_disappearance_does_not_claim_progress(self):
        monitor = ProgressSupervisor(); monitor.observe(snapshot(frame=0), action())
        s = snapshot(frame=239); s.enemies = []
        monitor.observe(s, action())
        self.assertEqual(monitor.last_progress, 0)
        s = s.model_copy(update={"frame": 240})
        self.assertEqual(monitor.observe(s, action()), "STALL_UNRECOVERED")

    def test_route_replanning_cannot_reset_clock(self):
        monitor = ProgressSupervisor()
        for frame in range(241):
            result = monitor.observe(snapshot(frame=frame), action(move=(1, 0), route_goal=[frame, 1], route_remaining=100))
        self.assertEqual(result, "STALL_UNRECOVERED")

    def test_real_route_progress_and_damage_reset_episode(self):
        monitor = ProgressSupervisor()
        for frame in range(200):
            s = snapshot(frame=frame); s.player.pos.x += frame
            self.assertIsNone(monitor.observe(s, action(move=(1, 0), route_goal=[1, 1], route_remaining=300-frame)))
        self.assertLess(monitor.max_no_progress, 10)

    def test_moving_in_circles_is_not_progress(self):
        monitor = ProgressSupervisor()
        for frame in range(241):
            s = snapshot(frame=frame); s.player.pos.x += (frame % 20)*2
            result = monitor.observe(s, action(move=(1, 0), shoot=(1, 0)))
        self.assertEqual(result, "STALL_UNRECOVERED")
