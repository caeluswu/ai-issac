import json
from pathlib import Path
import unittest
from unittest.mock import patch

from combat.models import WorldSnapshot
from combat.navigation import RoomExplorer
from tests.combat_fixtures import snapshot


class TestRoomExplorer(unittest.TestCase):
    def cases(self):
        return [[WorldSnapshot.model_validate(s) for s in v['snapshots']] for v in
                json.loads((Path(__file__).parent/'fixtures/empty_branch_navigation.json').read_text(encoding='utf8'))]

    def test_three_room_dead_end_returns_to_a_known_frontier(self):
        root, branch, end = self.cases()[0]
        explorer = RoomExplorer()
        self.assertIsNotNone(explorer.choose(root)); self.assertIsNotNone(explorer.choose(branch))
        route = explorer.choose(end)
        self.assertEqual(route[2]['target_room'], branch.room['room_idx'])
        self.assertEqual(explorer.reason, 'return_to_observed_frontier')
        branch.room_visit_id += ':return'
        route = explorer.choose(branch)
        self.assertEqual(route[2]['target_room'], 110)
        self.assertEqual(explorer.reason, 'unvisited_exit')

    def test_empty_first_branch_returns_to_start_then_takes_a_new_exit(self):
        root, dead_end = self.cases()[1]; explorer = RoomExplorer()
        explorer.choose(root)
        self.assertEqual(explorer.choose(dead_end)[2]['target_room'], root.room['room_idx'])
        root.room_visit_id += ':return'
        self.assertIn(explorer.choose(root)[2]['target_room'], (83, 85))

    def test_exhausted_observed_graph_does_not_bounce_forever(self):
        root, dead_end = self.cases()[1]
        root.layout['doors'] = {k:d for k,d in root.layout['doors'].items() if d['target_room'] == 97}
        explorer = RoomExplorer(); explorer.choose(root)
        self.assertIsNone(explorer.choose(dead_end))
        self.assertEqual(explorer.reason, 'observed_frontier_exhausted')

    def test_unreachable_first_hop_does_not_hide_an_alternative_frontier_path(self):
        s = snapshot(); s.room['room_idx'] = 0
        s.layout['doors'] = {str(i): {'target_room':i,'target_room_type':1,'is_open':True,'is_locked':False} for i in (1, 2)}
        explorer = RoomExplorer({0,1,2,3}); explorer.links = {1:{3},2:{3},3:{9}}
        def route(_, visited, *, target_room=None):
            return (0,2,{'target_room':2},[]) if target_room == 2 else None
        with patch('combat.navigation.door_route', side_effect=route): result = explorer.choose(s)
        self.assertEqual(result[2]['target_room'],2)

