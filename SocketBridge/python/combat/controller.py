"""Registered component execution and a 45-candidate receding-horizon solver."""
from collections import deque
from dataclasses import dataclass
import math
import statistics
import time
from .models import Vec, ZERO, BuildProfile, ActionFrame, Enemy
from .geometry import GeometryService, segment_distance
from .components import EXECUTABLES
from .threats import ThreatField, EnemyAdapters

MOVES = tuple((x, y) for y in (-1, 0, 1) for x in (-1, 0, 1))
SHOTS = ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))


class BuildResolver:
    def __init__(self):
        self.profile = BuildProfile()
        self.seen = set()
        self.speeds = deque(maxlen=60)
        self.initial_inventory = None
        self.previous = None

    def update(self, s):
        if s.stats["weapon_type"] != 1 or s.stats["player_type"] != 0:
            raise ValueError("UNSUPPORTED_BUILD: phase one requires Isaac with normal tears")
        fingerprint = sorted((s.inventory.get("collectibles") or {}).items())
        if self.initial_inventory is None: self.initial_inventory = fingerprint
        if fingerprint != self.initial_inventory: raise ValueError("BUILD_CHANGED: stop before using incompatible plan")
        p = self.profile
        p.move_speed = max(1, float(s.stats["speed"])*4.2)
        p.effective_range = max(40, float(s.stats["tear_range"])*.75)
        p.fire_interval = max(1, float(s.stats.get("max_fire_delay", 10))+1)
        p.inheritance_available = bool(s.movement_inheritance)
        for tear in s.projectiles["player_tears"] or []:
            key = (s.room_visit_id, tear.get("instance_id", tear["id"]))
            if key in self.seen: continue
            self.seen.add(key)
            # Only near-origin shots can be used to estimate base launch speed.
            if s.player.pos.distance(Vec(**tear["pos"])) < 35 and s.player.vel.length() < .5:
                self.speeds.append(Vec(**tear["vel"]).length())
        p.calibration_samples = len(self.speeds)
        p.calibrated = len(self.speeds) >= 3
        p.tear_speed = statistics.median(self.speeds) if self.speeds else 10*float(s.stats["shot_speed"])
        return p


@dataclass(frozen=True)
class EnemyForecast:
    identity: str
    start: Vec
    velocity: Vec
    radius: float
    uncertainty: float


class MotionPredictor:
    def forecast(self, s):
        return [EnemyForecast(e.instance_id, e.pos, e.vel, e.collision_radius,
                              4+min(8, e.vel.length()*2)) for e in s.hostiles if e.collision_class != 0]


class JointActionSolver:
    def __init__(self, registry=None, enemy_adapters=None):
        self.registry = registry or EXECUTABLES
        self.enemy_adapters = enemy_adapters or EnemyAdapters()
        self.modules = {}
        self.module_key = None
        self.recovery_level = 0
        self.route_failures = 0
        self.failed_goals = deque(maxlen=12)
        self.last_recovery_route = -1000
        self.last_route_goal = None
        self.geometry_key = None
        self.geometry = None
        self.target_id = None
        self.target_since = -100
        self.profile_name = None
        self.profile_since = -100
        self.previous_move = (0, 0)
        self.positions = deque(maxlen=65)
        self.recover_until = -1
        self.recoveries = []
        self.last_risk = 0.
        self.last_candidates = []
        self.max_hp = 1
        self.budget_exhausted = False

    def select_profile(self, s, plan, build):
        selected = plan.default_profile
        if self.profile_name in plan.profiles and (s.frame % 3 or s.frame-self.profile_since < 15):
            return self.profile_name, plan.profiles[self.profile_name]
        self.max_hp = max(self.max_hp, s.hp)
        target = next((e for e in s.hostiles if e.instance_id == self.target_id), None)
        features = {"attackable_count": sum(e.vulnerable for e in s.hostiles), "health_ratio": s.hp/self.max_hp,
                    "target_range_ratio": target.pos.distance(s.player.pos)/build.effective_range if target else 10000,
                    "risk": self.last_risk}
        for rule in plan.switch_rules:
            matches = []
            for p in rule.when:
                actual = sum(e.type == p.enemy_type for e in s.hostiles) if p.field == "enemy_type_count" else features[p.field]
                matches.append({"lt": actual < p.value, "le": actual <= p.value, "gt": actual > p.value,
                                "ge": actual >= p.value, "eq": actual == p.value}[p.op])
            if all(matches): selected = rule.use_profile; break
        if selected != self.profile_name:
            self.profile_name, self.profile_since = selected, s.frame
            self.positions.clear(); self.recover_until = -1
        return selected, plan.profiles[selected]

    def target(self, s, profile, build):
        candidates = [e for e in s.hostiles if e.vulnerable]
        if not candidates: self.target_id = None; return None
        p = profile.target.params
        module = self.registry.create(profile.target)
        def score(e): return module.score(e, s, build)
        best = max(candidates, key=lambda e: (score(e), e.instance_id))
        old = next((e for e in candidates if e.instance_id == self.target_id), None)
        if old and (s.frame-self.target_since < p["min_hold_ticks"] or score(best) < score(old)+p["switch_margin"]): best = old
        if best.instance_id != self.target_id: self.target_id, self.target_since = best.instance_id, s.frame
        return best

    def hit_score(self, s, build, profile, target, shot, velocity, geometry):
        if shot == (0, 0): return 0.
        return self.registry.create(profile.fire).hit_score(s, build, self.registry.create(profile.aim), target, shot, velocity, geometry)

    def solve(self, s, plan, build):
        started = time.perf_counter()
        name, profile = self.select_profile(s, plan, build)
        key = profile.model_dump_json()
        if key != self.module_key:
            self.modules = {slot: self.registry.create(getattr(profile, slot)) for slot in type(profile).model_fields}
            self.module_key = key
        modules = self.modules
        target = self.target(s, profile, build)
        geometry_key = (s.room_visit_id, s.stats["can_fly"], str(s.layout["grid"]), str(s.layout["cells"]))
        if geometry_key != self.geometry_key:
            self.geometry, self.geometry_key = GeometryService(s), geometry_key
        g = self.geometry
        if self.recovery_level and s.frame-self.last_recovery_route >= 45:
            if self.last_route_goal: self.failed_goals.append(self.last_route_goal)
            self.last_recovery_route = s.frame
            self.route_failures += 1
        route = g.firing_route(s.player.pos, target.pos, build.effective_range, excluded=self.failed_goals) if target else []
        if target and not route:
            # Normal tears can open an otherwise sealed lane through ordinary
            # poop. TNT, explosive/red poop and unknown variants are excluded.
            blockers = []
            for index, grid in (s.layout["grid"] or {}).items():
                if grid["type"] == 14 and grid["variant"] == 0 and grid["collision"] != 0:
                    position = Vec(x=grid["x"], y=grid["y"])
                    path = g.firing_route(s.player.pos, position, build.effective_range, terrain=True)
                    if path: blockers.append((len(path)*40+position.distance(s.player.pos), index, position, path))
            if blockers:
                _, index, position, route = min(blockers, key=lambda x: (x[0], x[1]))
                target = Enemy(instance_id="terrain:"+index, type=0, variant=0, pos=position, vel=ZERO,
                               hp=1, max_hp=1, collision_radius=18, vulnerable=True, friendly=False, dead=False, collision_class=0)
        # Route following supplies long-horizon progress to the short local
        # rollout. Without it a safe local optimum can strand the controller.
        route_goal = route[-1] if route else None
        self.last_route_goal = (round(route_goal.x), round(route_goal.y)) if route_goal else None
        while route and route[0].distance(s.player.pos) < (18 if len(route) > 1 else 5): route.pop(0)
        waypoint = route[0] if route else None
        threats = ThreatField(s, self.enemy_adapters)
        extra = profile.evade.params["extra_margin_px"]
        self.positions.append((s.frame, s.player.pos))
        history = [p for f, p in self.positions if f <= s.frame-profile.recovery.params["stall_window_ticks"]]
        if history and self.previous_move != (0, 0) and s.player.pos.distance(history[-1]) < 8 and s.frame > self.recover_until:
            self.recover_until = s.frame+profile.recovery.params["recovery_ticks"]
            self.recoveries.append(s.frame); self.positions.clear()
        recovering = s.frame < self.recover_until or self.recovery_level > 0
        horizon = profile.escape.params["horizon_ticks"]
        routes = {}
        candidates = []
        self.budget_exhausted = False
        for move in MOVES:
            if candidates and (time.perf_counter()-started)*1000 > 26:
                self.budget_exhausted = True
                break
            desired = Vec(x=move[0], y=move[1]).unit()*build.move_speed
            position, velocity = s.player.pos, s.player.vel
            trajectory = [position]
            closest_waypoint = s.player.pos.distance(waypoint) if waypoint else 0
            clearance, wall = 1000., False
            for tick in range(1, 13):
                velocity = velocity*.75+desired*.25
                new_position = position+velocity
                if not g.line_clear(position, new_position): wall = True; break
                clearance = min(clearance, threats.gap((position.x, position.y), (new_position.x, new_position.y),
                                                      tick, s.player.collision_radius+extra))
                position = new_position
                trajectory.append(position)
                if waypoint: closest_waypoint = min(closest_waypoint, position.distance(waypoint))
            cell = g.cell(position)
            if cell not in routes: routes[cell] = threats.escape(g, position, build.move_speed, s.player.collision_radius+extra)
            escape, slack, exit_directions = routes[cell]
            stance = 0.
            if target:
                delta = target.pos-position
                distance = delta.length()
                stance = modules["position"].score(delta, desired, s, build)
                if waypoint:
                    # Receding-horizon actions may brake or turn at the next
                    # update. Constant-input endpoint scoring penalizes crossing
                    # a nearby waypoint and makes zero input a local optimum.
                    stance += (s.player.pos.distance(waypoint)-closest_waypoint)*(.35 if recovering else .15)
            evade = modules["evade"].score(clearance, desired)
            for attack in modules["fire"].propose(s, build, profile, target, desired, g, modules["aim"]):
                shot, hit = attack.shoot, attack.hit_score
                postkill_safe = threats.attack_safe(target, build, trajectory, hit)
                damage_risk = max(0, -clearance)
                utility = hit*6+stance+evade+modules["escape"].score(escape)-(.04 if move != self.previous_move else 0)
                if wall: utility -= 100
                candidates.append({"move": move, "shoot": shot, "safe": not wall and clearance > 0 and postkill_safe and slack >= 6,
                                   "clearance": clearance, "wall": wall, "hit": hit, "escape": escape,
                                   "escape_slack": slack, "escape_directions": exit_directions,
                                   "utility": utility, "risk": damage_risk+(100 if not postkill_safe else 0), "fire_reason": attack.reason,
                                   "wait": attack.wait.__dict__ if attack.wait else None})
                if not postkill_safe:
                    candidates.append({**candidates[-1], "shoot": (0, 0), "hit": 0.,
                                       "safe": not wall and clearance > 0 and slack >= 6,
                                       "utility": utility-hit*6, "risk": damage_risk, "fire_reason": "death_effect_withdrawal"})
        # Safety is lexicographic. If every option is unsafe, minimize modelled
        # penetration before considering output; this is not a guarantee of life.
        best = max(candidates, key=lambda c: (c["safe"], not c["wall"], -c["risk"], c["utility"]))
        self.last_candidates = candidates
        self.previous_move = best["move"]
        self.last_risk = min(1., best["risk"]/30)
        return ActionFrame(frame=s.frame, room_visit_id=s.room_visit_id, move=best["move"], shoot=best["shoot"],
                           profile=name, target=target.instance_id if target else None,
                           state="RECOVER" if recovering else "EVADE" if best["clearance"] < 20 else "ENGAGE",
                           score={k: best[k] for k in ("clearance", "hit", "escape", "utility", "risk", "escape_slack")},
                           diagnostics={"route_goal": [round(route_goal.x), round(route_goal.y)] if route_goal else None,
                               "route_remaining": sum(a.distance(b) for a, b in zip([s.player.pos]+route, route)),
                               "no_safe_candidate": not any(c["safe"] for c in candidates), "fire_reason": best["fire_reason"], "wait": best["wait"],
                               "budget_exhausted": self.budget_exhausted, "evaluated_candidates": len(candidates)},
                           decision_ms=(time.perf_counter()-started)*1000)
