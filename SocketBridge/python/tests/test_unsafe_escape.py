import json
from pathlib import Path
import unittest
from combat.models import WorldSnapshot
from combat.controller import JointActionSolver, BuildResolver, select_joint_action
from combat.catalog import default_plan
from tests.combat_fixtures import context


class TestUnsafeEscape(unittest.TestCase):
    def candidate(self, early, escape, **updates):
        return dict(safe=False, wall=False, early_risk=early, escape_directions=escape,
                    risk=early, exposure=early*10, first_contact=1, utility=0, **updates)

    def test_small_model_difference_cannot_pin_player_without_escape(self):
        corner = self.candidate(45.5, 0)
        exit = self.candidate(48.1, 1)
        self.assertIs(select_joint_action([corner, exit]), exit)

    def test_contact_free_near_future_is_not_traded_for_predicted_contact(self):
        wait = self.candidate(0, 0)
        exit = self.candidate(5, 1)
        self.assertIs(select_joint_action([wait, exit]), wait)

    def test_escape_does_not_override_a_large_immediate_risk_increase(self):
        corner = self.candidate(10, 0)
        exit = self.candidate(35, 1)
        self.assertIs(select_joint_action([corner, exit]), corner)

    def test_recorded_corner_selects_available_eastward_withdrawal(self):
        raw = json.loads((Path(__file__).parent/"fixtures/corner_overlap.json").read_text(encoding="utf8"))
        s = WorldSnapshot.model_validate(raw); solver = JointActionSolver(); solver.emergency_until = s.frame+60
        action = solver.solve(s, default_plan(context(s)), BuildResolver().update(s))
        self.assertGreater(action.move[0], 0)
        self.assertGreater(action.score["escape"], 0)
        self.assertGreaterEqual(action.score["escape_slack"], 6)
        self.assertLess(action.diagnostics["blocked_steps"], 12)
