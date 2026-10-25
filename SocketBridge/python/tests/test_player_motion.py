import unittest
from combat.motion import PlayerMotion
from combat.controller import BuildResolver
from combat.geometry import GeometryService
from combat.models import Vec
from combat.threats import ThreatForecast
from tests.combat_fixtures import snapshot


class TestPlayerMotion(unittest.TestCase):
    def test_steady_player_displacement_doubles_api_velocity_not_firing_velocity(self):
        s = snapshot(); b = BuildResolver().update(s)
        v = Vec(x=4.4, y=0)
        p, api_velocity, _, _ = PlayerMotion.step(s.player.pos, v, v, GeometryService(s), b)
        self.assertAlmostEqual(p.x-s.player.pos.x, 8.8)
        self.assertEqual(api_velocity, v)
        self.assertAlmostEqual(b.move_speed, 8.8)

    def test_enemy_and_bullet_forecasts_do_not_use_player_scale(self):
        forecast = ThreatForecast("bullet", 100, 100, 8, 0, 1)
        self.assertLess(forecast.gap((108, 100), (108, 100), 1, 1), 0)
        self.assertGreater(forecast.gap((116, 100), (116, 100), 1, 1, begin=1), 0)

    def test_wall_block_returns_api_velocity_zero_in_blocked_axis(self):
        s = snapshot(); b = BuildResolver().update(s)
        _, v, blocked, unsafe = PlayerMotion.step(Vec(x=33, y=160), Vec(x=-4.4, y=0), Vec(x=-4.4, y=0), GeometryService(s), b)
        self.assertTrue(blocked); self.assertFalse(unsafe)
        self.assertEqual(v.x, 0)
