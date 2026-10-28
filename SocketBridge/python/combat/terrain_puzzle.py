"""Topology and observed phase control for enemy-free timed terrain rooms."""
from collections import defaultdict
import heapq
import math
import time

from .geometry import GeometryService
from .models import ActionFrame, Vec
from .motion import PlayerMotion


MOVES = ((0,0),(-1,0),(1,0),(0,-1),(0,1),(-1,-1),(1,-1),(-1,1),(1,1))
RETRACT_SETTLE_FRAMES = 4
EXTEND_GUARD_FRAMES = 6


class SpikePhaseModel:
    """Learn independent observed spike cycles, with bounded evidence history."""

    def __init__(self):
        self.states = {}
        self.transitions = defaultdict(list)
        self.periods = {}
        self.period = None  # Compatibility override for older replay fixtures.
        self.stable_since = {}
        self.last_frame = None
        self.revision = 0

    def observe(self, snapshot):
        if self.last_frame is not None and snapshot.frame-self.last_frame > 6:
            # Missing updates may hide a complete phase transition.
            self.states.clear(); self.transitions.clear(); self.periods.clear()
            self.stable_since.clear(); self.period = None; self.revision += 1
        self.last_frame = snapshot.frame
        live = set()
        for key, cell in (snapshot.layout["grid"] or {}).items():
            if cell["type"] != 9: continue
            index, state = int(key), cell["state"]
            live.add(index)
            previous = self.states.get(index)
            if previous is None or previous != state:
                self.stable_since[index] = snapshot.frame
                if previous is not None:
                    self.transitions[index].append((snapshot.frame, state))
                    self.transitions[index] = self.transitions[index][-12:]
                    intervals = []
                    for value in (0,1):
                        frames = [f for f,v in self.transitions[index] if v == value]
                        intervals.extend(b-a for a,b in zip(frames,frames[1:]))
                    if intervals and all(30 <= v <= 300 for v in intervals) and max(intervals)-min(intervals) <= 2:
                        self.periods[index] = sorted(intervals)[len(intervals)//2]
                    elif intervals:
                        self.periods.pop(index, None)
                        self.transitions[index] = self.transitions[index][-2:]
                        self.revision += 1
            self.states[index] = state
        for index in set(self.states)-live:
            self.states.pop(index, None); self.periods.pop(index, None)
            self.transitions.pop(index, None); self.stable_since.pop(index, None)
            self.revision += 1

    def cycle(self, index):
        return self.periods.get(index, self.period)

    def steady(self, index):
        # A completed mechanism may stop its spikes. Verify persistence beyond
        # the supported cycle range rather than trusting room-clear globally.
        return (self.states.get(index) == 1 and self.last_frame is not None and
                self.last_frame-self.stable_since.get(index,self.last_frame) >= 306)

    def phase(self, index):
        transitions = self.transitions[index]
        retracts = [frame for frame,state in transitions if state == 1]
        if not retracts or self.cycle(index) is None: return None
        durations = [next((later-frame for later,state in transitions if later > frame and state == 0),None)
                     for frame in retracts]
        durations = [duration for duration in durations if duration is not None]
        if not durations: return None
        return retracts[-1], min(durations)

    def safe(self, index, frame):
        if self.steady(index): return True
        phase = self.phase(index)
        if phase is None: return False
        retract, duration = phase
        elapsed = (frame-retract) % self.cycle(index)
        return RETRACT_SETTLE_FRAMES <= elapsed < duration-EXTEND_GUARD_FRAMES


def terrain_identity(snapshot):
    return (snapshot.run_id, snapshot.floor.instance_id if snapshot.floor else None,
            snapshot.room["room_idx"], tuple(sorted((int(i),c["variant"])
            for i,c in (snapshot.layout["grid"] or {}).items() if c["type"] == 9)))


class TimedTerrainPuzzle:
    """Stage on safe ground, then cross only cells forecast retracted."""

    def __init__(self, room_visit_id, known_cycles=None):
        self.room_visit_id = room_visit_id
        self.phase = SpikePhaseModel()
        self.phase.periods.update(known_cycles or {})
        self.staging_solver = None
        self.schedule = []
        self.schedule_goal = None
        self.solved = False
        self.schedule_key = None
        self.learning_started = None
        self.learning_goal = None

    def step(self, snapshot, build, goal, plate_index, plan):
        started = time.perf_counter()
        if snapshot.room_visit_id != self.room_visit_id:
            raise ValueError("PUZZLE_ROOM_CHANGED")
        if snapshot.hostiles or snapshot.bombs:
            raise ValueError("PUZZLE_ROOM_NOT_EMPTY")
        if self.learning_started is None: self.learning_started = snapshot.frame
        goal_identity = (goal.x, goal.y, plate_index)
        if goal_identity != self.learning_goal:
            self.learning_goal = goal_identity
            self.learning_started = snapshot.frame
        self.phase.observe(snapshot)
        timed = tuple(int(i) for i,c in (snapshot.layout["grid"] or {}).items() if c["type"] == 9)
        plate = (snapshot.layout["grid"] or {}).get(str(plate_index), {})
        solved = bool(snapshot.room["is_clear"] and plate.get("state") == 3 and
                      all(snapshot.layout["grid"][str(i)]["state"] == 1 for i in timed))
        if solved != self.solved:
            self.schedule = []
            self.solved = solved
            self.learning_started = snapshot.frame
        allowed = (plate_index,) if plate_index is not None else ()
        geometry = GeometryService(snapshot,allowed,(),timed)
        schedule_key = (build.move_speed, build.player_displacement_scale, geometry.physical_radius,
                        self.phase.revision, tuple(sorted(geometry.walkable)),
                        tuple((i, self.phase.cycle(i), self.phase.steady(i)) for i in timed))
        if schedule_key != self.schedule_key:
            self.schedule = []; self.schedule_key = schedule_key
        route = geometry.path(snapshot.player.pos,goal)
        if not route: raise RuntimeError("PUZZLE_NO_STATIC_TOPOLOGY")
        dynamic_route = [geometry.cell(point) for point in route if geometry.cell(point) in timed and not geometry.fly]
        if any(self.phase.phase(index) is None and not self.phase.steady(index) for index in dynamic_route):
            if snapshot.frame-self.learning_started >= 600:
                raise TimeoutError("PUZZLE_PHASE_LEARNING_TIMEOUT")
            first = next((n for n,point in enumerate(route) if geometry.cell(point) in timed),len(route))
            staging = route[max(0,first-1)] if route else snapshot.player.pos
            if snapshot.player.pos.distance(staging)>8:
                if self.staging_solver is None:
                    from .controller import JointActionSolver
                    self.staging_solver = JointActionSolver()
                action = self.staging_solver.solve(snapshot,plan,build,True,staging,allowed)
                action.state = "PUZZLE_STAGE"
                action.diagnostics["puzzle_phase"] = "learning"
                return action
            return self._action(snapshot,(0,0),"PUZZLE_OBSERVE",started,
                                {"puzzle_phase":"learning","period":self.phase.period,
                                 "wait":{"reason":"terrain_observation",
                                         "deadline_frame":self.learning_started+600,
                                         "evidence":"observed_type9_phase_incomplete"}})
        goal_cell = geometry.cell(goal)
        if self.schedule_goal != goal_cell or not self.schedule:
            self.schedule = self._plan(geometry,snapshot,build,timed,goal_cell)
            self.schedule_goal = goal_cell
        while len(self.schedule)>1 and snapshot.player.pos.distance(geometry.positions[self.schedule[0][0]]) < 11:
            self.schedule.pop(0)
        if not self.schedule:
            raise RuntimeError("PUZZLE_NO_TEMPORAL_ROUTE")
        next_cell, departure, arrival = self.schedule[0]
        target = geometry.positions[next_cell]
        if snapshot.frame > arrival + 8 and snapshot.player.pos.distance(target)>11:
            self.schedule = self._plan(geometry,snapshot,build,timed,goal_cell)
            next_cell, departure, arrival = self.schedule[0]
            target = geometry.positions[next_cell]
        if snapshot.frame < departure and geometry.cell(snapshot.player.pos) != next_cell:
            move, phase = (0,0), "wait_window"
        else:
            move = self._steer(geometry,snapshot,build,timed,target)
            phase = "cross" if move != (0,0) else "wait_window"
        return self._action(snapshot,move,"PUZZLE_TRAVERSE",started,{
            "puzzle_phase":phase,"periods":dict(self.phase.periods),
            "target":[target.x,target.y],"timed_route":dynamic_route,
            "scheduled_departure":departure,"scheduled_arrival":arrival,
            "route_goal":[goal.x,goal.y],
            "route_remaining":sum(a.distance(b) for a,b in zip([snapshot.player.pos]+route,route)),
            "wait":({"reason":"terrain_phase","deadline_frame":departure,
                     "evidence":"observed_phase_schedule"} if phase == "wait_window" else None)})

    def _plan(self, geometry, snapshot, build, timed, goal_cell):
        """Search the room's safe topology in (cell, cycle phase) space."""
        start = geometry.cell(snapshot.player.pos)
        if start not in geometry.walkable:
            raise RuntimeError("PUZZLE_START_NOT_WALKABLE")
        deadline = snapshot.frame+600
        speed = max(1., build.move_speed)
        duration = max(7, math.ceil(40/speed)+2)
        center_hazards = {cell:self._touched_timed(geometry,geometry.positions[cell],timed)
                          for cell in geometry.walkable}
        edge_hazards = {}
        for cell, neighbors in geometry.neighbors.items():
            a = geometry.positions[cell]
            for other in neighbors:
                b = geometry.positions[other]
                edge_hazards[cell,other] = [self._touched_timed(
                    geometry,a+(b-a)*(step/duration),timed) for step in range(1,duration+1)]
        def estimate(cell):
            delta = geometry.positions[cell]-geometry.positions[goal_cell]
            return (abs(delta.x)+abs(delta.y))/40*duration
        heap = [(snapshot.frame+estimate(start),snapshot.frame,0,start)]
        best = {(start,snapshot.frame):(snapshot.frame,0)}
        previous = {}
        finish = None
        while heap:
            _,frame,moves,cell = heapq.heappop(heap)
            key = (cell,frame)
            if best.get(key) != (frame,moves): continue
            if cell == goal_cell:
                finish = key;break
            for other, travel in ((cell,1),*((n,duration) for n in geometry.neighbors[cell])):
                hazard_steps = ([center_hazards[cell]] if other == cell else edge_hazards[cell,other])
                if any(any(not self._safe(h,frame+tick) for h in hazards)
                       for tick,hazards in enumerate(hazard_steps,1)):
                    continue
                next_frame = frame+travel
                if next_frame > deadline: continue
                next_key = (other,next_frame)
                cost=(next_frame,moves+(other!=cell))
                if cost >= best.get(next_key,(10**9,10**9)):continue
                best[next_key]=cost
                previous[next_key]=(key,frame,cell)
                heapq.heappush(heap,(next_frame+estimate(other),next_frame,cost[1],other))
        if finish is None:raise RuntimeError("PUZZLE_NO_TEMPORAL_ROUTE")
        path=[];key=finish
        while key in previous:
            before,depart,from_cell=previous[key]
            if key[0] != from_cell:path.append((key[0],depart,best[key][0]))
            key=before
        path.reverse()
        return path or [(goal_cell,snapshot.frame,snapshot.frame)]

    def _touched_timed(self, geometry, point, timed):
        if geometry.fly: return frozenset()
        radius = geometry.physical_radius
        offsets = ((0,0),(radius,0),(-radius,0),(0,radius),(0,-radius),
                   (radius*.707,radius*.707),(-radius*.707,radius*.707),
                   (radius*.707,-radius*.707),(-radius*.707,-radius*.707))
        return frozenset(geometry.cell_xy(point.x+dx,point.y+dy) for dx,dy in offsets
                         if geometry.cell_xy(point.x+dx,point.y+dy) in timed)

    def _safe(self, index, frame):
        return self.phase.safe(index, frame)

    def _steer(self, geometry, snapshot, build, timed, target):
        # Predict braking as well as the next input, so a cell center does not
        # induce a left/right oscillation at the boundary of a safe window.
        best = None
        for move in MOVES:
            position,velocity=snapshot.player.pos,snapshot.player.vel
            valid=True
            for tick in range(1,7):
                position,velocity,_,unsafe=PlayerMotion.step(position,velocity,
                    PlayerMotion.desired(move if tick<=2 else (0,0),build),geometry,build)
                if unsafe or not self._timed_safe(geometry,position,snapshot.frame+tick,timed):
                    valid=False;break
            if not valid:continue
            score = position.distance(target) + (0.1 if move != (0,0) else 0)
            if best is None or score < best[0]:best=(score,move)
        if best:return best[1]
        return self._escape(geometry,snapshot,build,timed,[target])

    def _timed_safe(self, geometry, point, frame, timed):
        if geometry.fly: return True
        radius = geometry.physical_radius
        offsets = ((0,0),(radius,0),(-radius,0),(0,radius),(0,-radius),
                   (radius*.707,radius*.707),(-radius*.707,radius*.707),
                   (radius*.707,-radius*.707),(-radius*.707,-radius*.707))
        touched = {geometry.cell_xy(point.x+dx,point.y+dy) for dx,dy in offsets}
        return all(self._safe(index,frame) for index in touched if index in timed)

    def _escape(self, geometry, snapshot, build, timed, route):
        options = []
        target = route[0]
        for move in MOVES[1:]:
            point, _, _, unsafe = PlayerMotion.step(snapshot.player.pos,snapshot.player.vel,
                PlayerMotion.desired(move,build),geometry,build)
            if unsafe or point.distance(snapshot.player.pos)<.1: continue
            hazard = not self._timed_safe(geometry,point,snapshot.frame+1,timed)
            options.append((hazard,point.distance(target),move))
        return min(options,key=lambda row:(row[0],row[1],MOVES.index(row[2])))[2] if options else (0,0)

    @staticmethod
    def _action(snapshot,move,state,started,diagnostics):
        return ActionFrame(frame=snapshot.frame,room_visit_id=snapshot.room_visit_id,
            move=move,shoot=(0,0),profile="terrain_puzzle",target=None,state=state,
            score={"clearance":0.,"risk":0.,"escape":0.,"escape_slack":0.,"hit":0.,"utility":0.},
            decision_ms=(time.perf_counter()-started)*1000,diagnostics=diagnostics)
