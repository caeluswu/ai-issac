"""Pure door navigation with the same local movement safety as combat."""
import time
from collections import deque
from .geometry import door_route
from .models import Vec, ActionFrame
from .controller import JointActionSolver
from .motion import PlayerMotion
from .threats import ThreatField


class RoomExplorer:
    """Find a first combat room through observed open ordinary doors only.

    Empty dead ends must return to an observed frontier rather than terminate
    just because their remaining exit has already been visited. Room indices
    identify map nodes; a new room visit still gets a fresh movement controller.
    """
    def __init__(self, visited=None):
        self.visited = visited if visited is not None else set()
        self.links = {}
        self.reason = None

    def choose(self, snapshot):
        room = snapshot.room['room_idx']
        self.visited.add(room)
        self.links[room] = {d['target_room'] for d in (snapshot.layout['doors'] or {}).values()
                            if d['is_open'] and not d['is_locked'] and d['target_room_type'] == 1}
        route = door_route(snapshot, self.visited)
        if route is not None:
            self.reason = 'unvisited_exit'
            return route
        queue, seen = deque([(room, None)]), {room}
        while queue:
            node, first = queue.popleft()
            if node != room and self.links.get(node, set())-self.visited:
                route = door_route(snapshot, self.visited, target_room=first)
                if route is not None:
                    self.reason = 'return_to_observed_frontier'
                    return route
            for neighbor in sorted(self.links.get(node, ())):
                if neighbor in self.visited and neighbor not in seen:
                    if node == room and door_route(snapshot, self.visited, target_room=neighbor) is None: continue
                    seen.add(neighbor)
                    queue.append((neighbor, neighbor if first is None else first))
        self.reason = 'observed_frontier_exhausted'
        return None


class DoorNavigator:
    def __init__(self, slot, door, allowed_devices=()):
        self.slot = slot
        self.goal = Vec(x=door["x"], y=door["y"])
        self.solver = JointActionSolver()
        self.allowed_devices = allowed_devices

    def step(self, s, plan, build):
        began = time.perf_counter()
        delta = self.goal-s.player.pos
        cross = abs(delta.y) if self.slot % 2 == 0 else abs(delta.x)
        door = (s.layout["doors"] or {}).get(str(self.slot), {})
        if delta.length() <= 45 and cross < 8 and door.get("is_open") and not door.get("is_locked"):
            # The selected open door is an observed portal outside the room's
            # walkable grid. Check the next physical update before crossing it;
            # the next room must be classified from a fresh snapshot.
            move = ((-1, 0), (0, -1), (1, 0), (0, 1))[self.slot % 4]
            velocity = s.player.vel*.75+PlayerMotion.desired(move, build)*.25
            end = s.player.pos+velocity*build.player_displacement_scale
            field = ThreatField(s, self.solver.enemy_adapters)
            gap = field.gap((s.player.pos.x, s.player.pos.y), (end.x, end.y), 1, s.player.collision_radius+8)
            if gap > 0:
                return ActionFrame(frame=s.frame, room_visit_id=s.room_visit_id, move=move, shoot=(0, 0),
                    profile=plan.default_profile, target=None, state="NAVIGATE_PORTAL",
                    score={"clearance": gap, "risk": 0, "escape": 0, "escape_slack": 0, "hit": 0, "utility": 0},
                    decision_ms=(time.perf_counter()-began)*1000,
                    diagnostics={"route_goal": [self.goal.x, self.goal.y], "route_remaining": delta.length(),
                                 "movement_target": "door:"+str(self.slot), "no_safe_candidate": False, "blocked_steps": 0,
                                 "fire_reason": "navigation_move_only", "portal_exit": True})
        action = self.solver.solve(s, plan, build, True, self.goal, self.allowed_devices)
        action.state = "NAVIGATE"
        action.diagnostics["movement_target"] = "door:"+str(self.slot)
        return action
