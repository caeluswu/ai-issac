"""Exact-query optimizations must preserve conservative collision decisions."""
from collections import deque
import json
from pathlib import Path
import random
import unittest
from unittest.mock import patch

from combat.controller import BuildResolver, JointActionSolver
from combat.geometry import GeometryService
from combat.models import BattlePlan, Vec, WorldSnapshot
from combat.threats import ThreatField
from tests.combat_fixtures import snapshot


def exhaustive_route(g, start, target, effective_range):
    origin = g.nearest(start)
    queue, parents, depth, best = deque([origin]), {}, {origin: 0}, None
    while queue:
        i = queue.popleft(); p = g.positions[i]
        for candidate in (Vec(x=p.x, y=target.y), Vec(x=target.x, y=p.y)):
            if candidate.distance(p) > 19: continue
            distance = (target-candidate).length()
            if 60 <= distance <= effective_range*.85 and g.clear(candidate) and g.line_clear(p, candidate) and g.line_clear(candidate, target, shot=True):
                score = depth[i]*40+p.distance(candidate)+abs(distance-effective_range*.55)*.15
                if best is None or score < best[0]: best = (score, i, candidate)
        for j in g.neighbors.get(i, []):
            if j not in depth: depth[j], parents[j] = depth[i]+1, i; queue.append(j)
    if best is None: return []
    path, i = [best[2]], best[1]
    while True:
        path.append(g.positions[i])
        if i not in parents: break
        i = parents[i]
    return list(reversed(path))


class TestExactLatencyPaths(unittest.TestCase):
    def test_pruned_firing_stance_matches_full_search_in_obstructed_rooms(self):
        rng = random.Random(4711)
        for _ in range(20):
            s = snapshot()
            for i, cell in s.layout['cells'].items():
                if cell['inside'] and int(i) not in (67, 70) and rng.random() < .12: cell['collision'] = 4
            g = GeometryService(s)
            target = Vec(x=rng.uniform(60, 500), y=rng.uniform(60, 260))
            distance = rng.uniform(100, 500)
            self.assertEqual(g.firing_route(s.player.pos, target, distance), exhaustive_route(g, s.player.pos, target, distance))

    def test_exact_line_cache_separates_radius_weapon_and_geometry_and_is_bounded(self):
        s = snapshot(); g = GeometryService(s)
        a, b = Vec(x=40, y=21), Vec(x=120, y=21)
        self.assertTrue(g.line_clear(a, b, shot=True))
        self.assertFalse(g.line_clear(a, b, radius=10))
        self.assertTrue(g.line_clear(a, b, radius=0))
        with patch.object(g, 'cell_xy', side_effect=AssertionError('cached ray recomputed')):
            self.assertTrue(g.line_clear(a, b, shot=True))
            self.assertFalse(g.line_clear(a, b, radius=10))
        for i in range(4100): g.line_clear(Vec(x=-1000-i, y=0), b)
        self.assertLessEqual(len(g.line_cache), 4096)
        s.layout['cells']['16']['collision'] = 4
        self.assertFalse(GeometryService(s).line_clear(a, b, shot=True))

    def test_tiny_threat_exact_path_keeps_future_and_distant_threats(self):
        s = snapshot(); field = ThreatField(s)
        for tick in (0, 3, 12, 30, 45):
            for a, b in (((280, 160), (410, 160)), ((80, 80), (100, 80))):
                expected = any(t.gap(a, b, tick, 20, max(0, tick-3)) <= 0 for t in field.threats)
                self.assertEqual(field.collides(a, b, tick, 20, max(0, tick-3)), expected)

    def test_captured_slow_frame_uses_complete_candidate_set(self):
        raw = json.loads((Path(__file__).parent/'fixtures/latency_joint.json').read_text(encoding='utf8'))
        s = WorldSnapshot.model_validate(raw['snapshot']); plan = BattlePlan.model_validate(raw['plan'])
        solver = JointActionSolver(); action = solver.solve(s, plan, BuildResolver().update(s))
        self.assertFalse(action.diagnostics['budget_exhausted'])
        self.assertGreaterEqual(action.diagnostics['evaluated_candidates'], 9)

