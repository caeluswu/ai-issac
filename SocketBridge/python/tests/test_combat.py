import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
from combat.models import Vec, WorldSnapshot
from combat.catalog import compile_plan, default_plan
from combat.geometry import GeometryService, segment_distance, door_route
from combat.controller import JointActionSolver, BuildResolver
from combat.observation import SnapshotAssembler, CombatJudge
from combat.planner import Planner, KnowledgeStore
from facade import SocketBridge
from tests.combat_fixtures import snapshot, context


class TestContracts(unittest.TestCase):
    def setUp(self): self.s = snapshot(); self.ref = context(self.s)

    def test_default_compiles(self):
        self.assertEqual(len(type(default_plan(self.ref).profiles["safe"]).model_fields), 7)

    def test_reject_unknown_component_and_parameter(self):
        for field, value in (("component", "target.exec"), ("params", {"exec": "arbitrary"})):
            p = default_plan(self.ref).model_dump(); p["profiles"]["safe"]["target"][field] = value
            with self.assertRaises(ValueError): compile_plan(p, self.ref)

    def test_reject_bounds_and_types(self):
        for value in (-1, 40, True, "10", float("nan")):
            p = default_plan(self.ref).model_dump(); p["profiles"]["safe"]["target"]["params"]["min_hold_ticks"] = value
            with self.assertRaises(ValueError): compile_plan(p, self.ref)

    def test_reject_range_inversion(self):
        p = default_plan(self.ref).model_dump(); p["profiles"]["safe"]["position"]["params"].update(min_range_ratio=.9, max_range_ratio=.3)
        with self.assertRaises(ValueError): compile_plan(p, self.ref)

    def test_stale_context_but_snapshot_progress_allowed(self):
        p = default_plan(self.ref).model_dump(); p["context_ref"]["snapshot_id"] = "older-frame"
        compile_plan(p, self.ref)
        for key, value in (("room_visit_id", "other"), ("build_revision", 2), ("plan_revision", 1)):
            q = copy.deepcopy(p); q["context_ref"][key] = value
            with self.assertRaises(ValueError): compile_plan(q, self.ref)

    def test_missing_observation_never_becomes_zero(self):
        d = self.s.model_dump(); del d["health"]["red_hearts"]
        with self.assertRaises(ValueError): WorldSnapshot.model_validate(d)

    def test_frame_dedup_room_and_partial_messages(self):
        a = SnapshotAssembler(); raw = {"payload": {"COMBAT_FRAME": self.s.model_dump()}}
        self.assertIsNotNone(a.consume(raw)); self.assertIsNone(a.consume(raw))
        self.assertIsNone(a.consume({"payload": {"ENEMIES": []}}))
        raw["payload"]["COMBAT_FRAME"]["room_visit_id"] = "test:2"
        self.assertEqual(a.consume(raw).room_visit_id, "test:2")

    def test_clear_requires_game_and_enemy_agreement(self):
        judge = CombatJudge()
        self.s.room["is_clear"] = True; self.s.room["enemy_count"] = 0
        self.s.enemies[0].vulnerable = False
        for _ in range(5): self.assertIsNone(judge.observe(self.s))
        empty = snapshot(enemies=False)
        self.assertIsNone(judge.observe(empty)); self.assertIsNone(judge.observe(empty))
        self.assertEqual(judge.observe(empty), "CLEARED")

    def test_healing_does_not_cancel_damage(self):
        judge = CombatJudge(); judge.observe(self.s)
        hurt = self.s.model_copy(deep=True); hurt.health["red_hearts"] = 4
        judge.observe(hurt); judge.observe(self.s)
        self.assertEqual(judge.damage, 2)

    def test_non_blocking_hazard_remains_a_threat_after_clear(self):
        judge = CombatJudge()
        self.s.room.update(is_clear=True, enemy_count=0)
        self.s.enemies[0].vulnerable = False
        self.s.enemies[0].can_shut_doors = False
        for frame in range(3):
            self.s.frame = frame
            result = judge.observe(self.s)
        self.assertEqual(result, "CLEARED")
        self.assertEqual(len(self.s.hostiles), 1)

    def test_non_blocking_entity_cannot_override_uncleared_game(self):
        self.s.enemies[0].can_shut_doors = False
        judge = CombatJudge()
        for _ in range(5): self.assertIsNone(judge.observe(self.s))


class TestGeometryAndControl(unittest.TestCase):
    def setUp(self):
        self.s = snapshot(); self.b = BuildResolver().update(self.s); self.p = default_plan(context(self.s))

    def test_pit_is_shootable_not_walkable(self):
        i = str(4*15+8); self.s.layout["cells"][i]["collision"] = 1
        g = GeometryService(self.s)
        self.assertFalse(g.clear(Vec(x=320, y=160))); self.assertTrue(g.clear(Vec(x=320, y=160), shot=True))

    def test_wall_occludes_shot(self):
        self.s.layout["cells"][str(4*15+8)]["collision"] = 4
        g = GeometryService(self.s)
        self.assertFalse(g.line_clear(self.s.player.pos, self.s.enemies[0].pos, shot=True))
        self.assertGreater(len(g.path(self.s.player.pos, self.s.enemies[0].pos)), 4)

    def test_shape_void_not_treated_as_open(self):
        self.s.layout["cells"]["68"]["inside"] = False
        self.assertFalse(GeometryService(self.s).clear(Vec(x=320, y=160), shot=True))

    def test_swept_collision_crossing(self):
        self.assertEqual(segment_distance(Vec(x=0, y=0), Vec(x=-20, y=0), Vec(x=20, y=0)), 0)

    def test_door_only_current_open_unvisited(self):
        self.assertEqual(door_route(self.s, {84})[1], 2)
        self.assertIsNone(door_route(self.s, {84, 85}))

    def test_firing_at_stationary_enemy(self):
        solver = JointActionSolver(); action = solver.solve(self.s, self.p, self.b)
        self.assertEqual(action.shoot, (1, 0)); self.assertGreater(action.score["hit"], 0)

    def test_short_lead_does_not_shorten_tear_range(self):
        profile = self.p.profiles["safe"]
        profile.aim.params.update(max_lead_ticks=3, movement_inheritance=False)
        self.s.enemies[0].pos = self.s.player.pos+Vec(x=150, y=0)
        for aim in ("aim.direct", "aim.predictive"):
            profile.aim.component = aim
            score = JointActionSolver().hit_score(self.s, self.b, profile, self.s.enemies[0],
                                                  (1, 0), Vec(x=0, y=0), GeometryService(self.s))
            self.assertGreater(score, 0)

    def test_prediction_changes_moving_target_score(self):
        solver = JointActionSolver(); profile = self.p.profiles["safe"]
        self.s.enemies[0].pos.y = 180; self.s.enemies[0].vel.y = -2
        g = GeometryService(self.s)
        predictive = solver.hit_score(self.s, self.b, profile, self.s.enemies[0], (1, 0), Vec(x=0, y=0), g)
        profile.aim.component = "aim.direct"
        direct = solver.hit_score(self.s, self.b, profile, self.s.enemies[0], (1, 0), Vec(x=0, y=0), g)
        self.assertGreater(predictive, direct)

    def test_movement_inheritance_changes_hit(self):
        solver = JointActionSolver(); profile = self.p.profiles["safe"]
        self.s.enemies[0].pos.y = 180
        g = GeometryService(self.s)
        inherited = solver.hit_score(self.s, self.b, profile, self.s.enemies[0], (1, 0), Vec(x=0, y=4), g)
        profile.aim.params["movement_inheritance"] = False
        direct = solver.hit_score(self.s, self.b, profile, self.s.enemies[0], (1, 0), Vec(x=0, y=4), g)
        self.assertGreater(inherited, direct)

    def test_invulnerable_enemy_still_threat_not_target(self):
        self.s.enemies[0].vulnerable = False
        action = JointActionSolver().solve(self.s, self.p, self.b)
        self.assertIsNone(action.target); self.assertEqual(action.shoot, (0, 0))

    def test_dead_target_reselected_without_hold(self):
        solver = JointActionSolver(); solver.solve(self.s, self.p, self.b)
        self.s.enemies[0].dead = True; self.s.frame += 1
        self.assertIsNone(solver.solve(self.s, self.p, self.b).target)

    def test_friendly_tear_not_a_danger(self):
        a = JointActionSolver().solve(self.s, self.p, self.b)
        self.s.projectiles["friendly_tears"] = [{"pos": self.s.player.pos.model_dump()}]
        b = JointActionSolver().solve(self.s, self.p, self.b)
        self.assertEqual(a.move, b.move); self.assertEqual(a.score, b.score)

    def test_incoming_projectile_changes_action(self):
        self.s.projectiles["enemy_projectiles"] = [{"pos": {"x": 330, "y": 160}, "vel": {"x": -8, "y": 0}, "collision_radius": 6}]
        action = JointActionSolver().solve(self.s, self.p, self.b)
        self.assertNotEqual(action.move[1], 0)

    def test_build_change_rejected(self):
        resolver = BuildResolver(); resolver.update(self.s)
        self.s.inventory["collectibles"]["118"] = 1
        with self.assertRaisesRegex(ValueError, "BUILD_CHANGED"): resolver.update(self.s)

    def test_sealed_enemy_requires_breaking_plain_poop(self):
        for y in range(1, 8): self.s.layout["cells"][str(y*15+8)]["collision"] = 3
        self.s.layout["grid"]["68"] = {"type": 14, "variant": 0, "collision": 3, "x": 320, "y": 160}
        action = JointActionSolver().solve(self.s, self.p, self.b)
        self.assertEqual(action.target, "terrain:68")
        self.assertEqual(action.shoot, (1, 0))


class TestPlanner(unittest.IsolatedAsyncioTestCase):
    async def test_valid_two_stage_and_validation_repair(self):
        s = snapshot(); ref = context(s)
        assessment = {"context_ref": ref.model_dump(), "enemy_groups": [], "primary_dangers": [], "terrain_constraints": [],
                      "weapon_opportunities": [], "recommended_tactics": [], "unknowns": []}
        plan = default_plan(ref).model_dump()
        bad = copy.deepcopy(plan); bad["profiles"]["safe"]["position"]["params"]["min_range_ratio"] = 4
        client = type("Client", (), {"complete": AsyncMock(side_effect=[(json.dumps(x), {}) for x in (assessment, bad, plan)])})()
        events = []
        p = Planner(client, None, lambda kind, **data: events.append(kind))
        result = await p.plan(s, BuildResolver().update(s))
        self.assertEqual(result.default_profile, "safe")
        self.assertIn("llm_validation_error", events)
        self.assertEqual(client.complete.await_count, 3)

    async def test_invalid_json_budget(self):
        client = type("Client", (), {"complete": AsyncMock(return_value=("{broken", {}))})()
        p = Planner(client, None, lambda *a, **k: None)
        with self.assertRaises(ValueError): await p.plan(snapshot(), BuildResolver().update(snapshot()))
        self.assertEqual(client.complete.await_count, 3)

    async def test_timeout_propagates_for_runtime_fallback(self):
        client = type("Client", (), {"complete": AsyncMock(side_effect=TimeoutError)})()
        with self.assertRaises(TimeoutError): await Planner(client, None, lambda *a, **k: None).plan(snapshot(), BuildResolver().update(snapshot()))

    async def test_unknown_mechanism_not_fabricated(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "index.json").write_text("{}")
            result = await KnowledgeStore(Path(d)).lookup(9999)
            self.assertEqual(result["status"], "unknown"); self.assertEqual(result["facts"], [])

    async def test_complete_input_releases_all_buttons(self):
        bridge = SocketBridge(); bridge._server.send = AsyncMock(return_value=True)
        await bridge.send_input()
        data = bridge._server.send.call_args.args[0]
        self.assertEqual(data["move"], {"x": 0, "y": 0})
        self.assertEqual(data["shoot"], {"x": 0, "y": 0})
        self.assertTrue(all(data[k] is False for k in ("use_bomb", "use_item", "use_card", "use_pill", "drop")))


if __name__ == "__main__": unittest.main()
