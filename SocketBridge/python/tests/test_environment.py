import json
from pathlib import Path
import unittest
from combat.models import WorldSnapshot, Vec, BuildProfile
from combat.environment import EnvironmentController
from combat.terrain_puzzle import SpikePhaseModel, TimedTerrainPuzzle
from combat.navigation import DoorNavigator
from combat.geometry import GeometryService
from combat.observation import SnapshotAssembler
from combat.threats import ThreatField
from combat.controller import JointActionSolver, BuildResolver
from combat.catalog import default_plan
from tests.combat_fixtures import context


class TestEnvironment(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((Path(__file__).parent/"fixtures/environment_tnt.json").read_text(encoding="utf8"))
        self.s = WorldSnapshot.model_validate(self.raw[0])

    def test_locked_empty_plate_room_is_not_combat(self):
        self.assertFalse(self.s.blocking_combat)
        self.assertFalse(self.s.room["is_clear"])
        self.assertEqual(EnvironmentController().objective(self.s).kind, "explosion_withdrawal")

    def test_unknown_fuse_and_tnt_chain_remain_dangerous_at_horizon(self):
        field = ThreatField(self.s)
        bomb = self.s.bombs[0]["pos"]
        self.assertLess(field.gap((bomb["x"], bomb["y"]), (bomb["x"], bomb["y"]), 40, 10), 0)
        self.assertTrue(any(t.source == "potential_tnt_chain" for t in field.threats))
        self.s.bombs = []
        self.assertFalse(any(t.source == "potential_tnt_chain" for t in ThreatField(self.s).threats))

    def test_controller_withdraws_from_existing_blast_overlap(self):
        a = JointActionSolver().solve(self.s, default_plan(context(self.s)), BuildResolver().update(self.s), True)
        self.assertNotEqual(a.move, (0, 0))
        self.assertGreater(a.move[1], 0)
        self.assertEqual(a.shoot, (0, 0))

    def test_knockback_boundary_can_move_out_but_not_deeper(self):
        s = WorldSnapshot.model_validate(self.raw[1]); g = GeometryService(s)
        origin = s.player.pos
        p, _, _, unsafe = g.sweep(origin, Vec(x=3, y=0))
        self.assertGreater(p.x, origin.x); self.assertFalse(unsafe)
        p, _, _, _ = g.sweep(origin, Vec(x=-3, y=0))
        self.assertEqual(p.x, origin.x)

    def test_plate_permission_is_local_and_does_not_unlock_spikes(self):
        self.s.bombs = []
        for index, grid in self.s.layout["grid"].items():
            if grid["type"] == 12:
                grid.update(collision=0, state=4)
                self.s.layout["cells"][index]["collision"] = 0
        objective = EnvironmentController().objective(self.s)
        self.assertEqual(objective.kind, "activate_pressure_plate")
        self.assertFalse(GeometryService(self.s).clear(objective.goal))
        self.assertTrue(GeometryService(self.s, objective.allowed_devices).clear(objective.goal))
        self.s.layout["grid"]["22"]["type"] = 8
        self.assertFalse(GeometryService(self.s, objective.allowed_devices).clear(objective.goal))

    def test_destroyed_tnt_retains_short_transition_hazard(self):
        assembler = SnapshotAssembler()
        assembler.consume({"payload": {"COMBAT_FRAME": self.raw[0]}})
        nxt = json.loads(json.dumps(self.raw[0])); nxt["frame"] += 1
        nxt["layout"]["grid"]["18"]["state"] = 4; nxt["layout"]["grid"]["18"]["collision"] = 0
        s = assembler.consume({"payload": {"COMBAT_FRAME": nxt}})
        self.assertEqual(s.environment_residuals[0]["id"], "18")
        nxt["frame"] += 7
        s = assembler.consume({"payload": {"COMBAT_FRAME": nxt}})
        self.assertEqual(s.environment_residuals, [])


class TestTimedTerrainPuzzle(unittest.TestCase):
    def setUp(self):
        raw = json.loads((Path(__file__).parent/"fixtures/necropolis_plate961.json").read_text(encoding="utf8"))
        self.s = WorldSnapshot.model_validate(raw)
        self.build = BuildProfile(move_speed=8.8, player_displacement_scale=2)
        self.goal = Vec(x=320,y=280)

    def test_whole_room_topology_excludes_permanent_spikes(self):
        objective = EnvironmentController().objective(self.s)
        self.assertEqual(objective.kind, "timed_pressure_plate")
        timed = tuple(int(i) for i,c in self.s.layout["grid"].items() if c["type"] == 9)
        geometry = GeometryService(self.s,(67,),(),timed)
        route = geometry.path(self.s.player.pos,self.goal)
        self.assertTrue(route)
        self.assertTrue(any(geometry.cell(p) in timed for p in route))
        self.assertFalse(any(self.s.layout["grid"].get(str(geometry.cell(p)),{}).get("type") == 8
                             for p in route))
        self.assertFalse(GeometryService(self.s,(67,),(),timed).clear(Vec(x=280,y=320)))

    def test_observed_phase_must_match_safe_window(self):
        model = SpikePhaseModel()
        grid = self.s.layout["grid"]
        for frame in range(1,129):
            self.s.frame = frame
            grid["47"]["state"] = 0 if frame < 8 or 68 <= frame < 128 else 1
            model.observe(self.s)
        self.assertEqual(model.cycle(47),120)
        self.assertFalse(model.safe(47,130))
        self.assertTrue(model.safe(47,140))
        self.assertFalse(model.safe(47,185))
        self.assertFalse(model.safe(47,190))

    def test_temporal_route_and_solved_exit_route(self):
        puzzle = TimedTerrainPuzzle(self.s.room_visit_id)
        puzzle.phase.period = 120
        for index,cell in self.s.layout["grid"].items():
            if cell["type"] == 9:
                puzzle.phase.transitions[int(index)] = [(0,1),(60,0),(120,1)]
        self.s.frame = 150
        action = puzzle.step(self.s,self.build,self.goal,67,None)
        self.assertEqual(action.shoot,(0,0))
        self.assertTrue(puzzle.schedule)
        self.assertEqual(puzzle.schedule[-1][0],67)
        self.assertTrue(all(self.s.layout["grid"].get(str(cell),{}).get("type") != 8
                            for cell,_,_ in puzzle.schedule))
        self.s.room["is_clear"] = True
        self.s.layout["grid"]["67"]["state"] = 3
        for cell in self.s.layout["grid"].values():
            if cell["type"] == 9:cell["state"] = 1
        # Room clear alone must not disable future hazard checks. Verify that
        # each spike actually stays retracted beyond the supported cycle range.
        for frame in range(self.s.frame+1,self.s.frame+308):
            self.s.frame = frame
            puzzle.phase.observe(self.s)
        action = puzzle.step(self.s,self.build,Vec(x=320,y=160),67,default_plan(context(self.s)))
        self.assertTrue(puzzle.solved)
        self.assertEqual(puzzle.schedule[-1][0],22)
        self.assertEqual(action.shoot,(0,0))

    def test_bottom_entry_has_future_temporal_route(self):
        self.s.frame = 172
        self.s.player.pos = Vec(x=320,y=364)
        puzzle = TimedTerrainPuzzle(self.s.room_visit_id)
        puzzle.phase.period = 120
        for index,retract in ((98,112),(83,100),(68,88)):
            puzzle.phase.transitions[index] = [(retract,1),(retract+60,0)]
        timed = tuple(int(i) for i,c in self.s.layout["grid"].items() if c["type"] == 9)
        geometry = GeometryService(self.s,(67,),(),timed)
        schedule = puzzle._plan(geometry,self.s,self.build,timed,67)
        self.assertEqual([cell for cell,_,_ in schedule],[98,83,68,67])
        self.assertGreaterEqual(schedule[0][1],self.s.frame)

    def test_revisited_solved_room_rebuilds_navigation_puzzle(self):
        self.s.room["is_clear"] = True
        self.s.layout["grid"]["67"]["state"] = 3
        for cell in self.s.layout["grid"].values():
            if cell["type"] == 9:cell["state"] = 1
        door = {"x":320,"y":120,"is_open":True,"is_locked":False}
        self.s.layout["doors"]["1"] = door
        navigator = DoorNavigator(1,door)
        action = navigator.step(self.s,default_plan(context(self.s)),self.build)
        self.assertIsNotNone(navigator.puzzle)
        self.assertEqual(action.diagnostics["puzzle_phase"],"learning")
        self.assertEqual(action.shoot,(0,0))

    def test_independent_cycles_and_missing_updates(self):
        model = SpikePhaseModel()
        for frame in range(1,361):
            self.s.frame = frame
            for index,period in ((47,120),(48,90)):
                self.s.layout["grid"][str(index)].update(type=9,state=int(frame % period < period//2))
            model.observe(self.s)
        self.assertEqual(model.cycle(47),120)
        self.assertEqual(model.cycle(48),90)
        self.s.frame += 20
        model.observe(self.s)
        self.assertIsNone(model.cycle(47))
        self.assertFalse(model.safe(47,self.s.frame+1))

    def test_completed_plate_is_not_selected_again(self):
        self.s.layout["grid"]["67"]["state"] = 3
        self.assertEqual(EnvironmentController().objective(self.s).kind,"unknown_environment")

    def test_clear_room_without_plate_still_uses_temporal_navigation(self):
        self.s.room["is_clear"] = True
        self.s.layout["grid"] = {i:c for i,c in self.s.layout["grid"].items() if c["type"] != 20}
        door = {"x":320,"y":120,"is_open":True,"is_locked":False}
        self.s.layout["doors"]["1"] = door
        nav = DoorNavigator(1,door)
        action = nav.step(self.s,default_plan(context(self.s)),self.build)
        self.assertIsNotNone(nav.puzzle)
        self.assertEqual(action.shoot,(0,0))

    def test_next_untriggered_plate_is_selected(self):
        self.s.layout["grid"]["67"]["state"] = 3
        cell = self.s.layout["cells"]["22"]
        self.s.layout["grid"]["22"] = dict(type=20,variant=0,state=0,collision=0,x=cell["x"],y=cell["y"])
        self.assertEqual(EnvironmentController().objective(self.s).allowed_devices,(22,))

    def test_temporal_path_supports_different_periods_on_successive_cells(self):
        self.s.frame = 172
        self.s.player.pos = Vec(x=320,y=364)
        puzzle = TimedTerrainPuzzle(self.s.room_visit_id)
        for index,retract,period in ((98,112,120),(83,100,100),(68,88,90)):
            puzzle.phase.periods[index] = period
            puzzle.phase.transitions[index] = [(retract,1),(retract+60,0)]
        timed = tuple(int(i) for i,c in self.s.layout["grid"].items() if c["type"] == 9)
        geometry = GeometryService(self.s,(67,),(),timed)
        schedule = puzzle._plan(geometry,self.s,self.build,timed,67)
        self.assertEqual(schedule[-1][0],67)
        for cell,depart,arrival in schedule:
            if cell in timed:
                self.assertTrue(puzzle.phase.safe(cell,arrival))
        self.assertTrue(any(right[1] > left[2] for left,right in zip(schedule,schedule[1:])))

    def test_new_objective_gets_its_own_learning_deadline(self):
        puzzle = TimedTerrainPuzzle(self.s.room_visit_id)
        puzzle.learning_started = self.s.frame-599
        puzzle.learning_goal = (320,400,67)
        action = puzzle.step(self.s,self.build,self.goal,67,default_plan(context(self.s)))
        self.assertEqual(puzzle.learning_started,self.s.frame)
        self.assertEqual(action.shoot,(0,0))

    def test_reentry_reuses_period_but_never_stale_phase(self):
        puzzle = TimedTerrainPuzzle(self.s.room_visit_id,{47:120})
        self.assertEqual(puzzle.phase.cycle(47),120)
        self.assertIsNone(puzzle.phase.phase(47))
        self.assertFalse(puzzle.phase.safe(47,self.s.frame))
