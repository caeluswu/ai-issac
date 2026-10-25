"""Registered component execution and a 45-candidate receding-horizon solver."""
from collections import deque
from dataclasses import dataclass
import math
import statistics
import time
from .models import Vec, ZERO, BuildProfile, ActionFrame, Enemy
from .geometry import GeometryService, segment_distance
from .components import EXECUTABLES, resilient_component
from .threats import ThreatField, EnemyAdapters, SafetyAssessment

MOVES = tuple((x, y) for y in (-1, 0, 1) for x in (-1, 0, 1))
SHOTS = ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))


class NormalTearsBuild:
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


class BuildResolver:
    """Dispatch observation calibration to a registered weapon build adapter."""
    def __init__(self, adapters=None):
        self.adapters = {1: NormalTearsBuild()} if adapters is None else adapters
        self.weapon_type = None
    def update(self, s):
        weapon = s.stats["weapon_type"]
        if weapon not in self.adapters: raise ValueError("UNSUPPORTED_BUILD: unregistered weapon adapter")
        if self.weapon_type is not None and weapon != self.weapon_type: raise ValueError("BUILD_CHANGED: weapon adapter changed")
        self.weapon_type = weapon
        return self.adapters[weapon].update(s)


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
        module = self.modules.get("target") or self.registry.create(profile.target)
        pending = getattr(self.modules.get("fire"), "pending_damage", lambda s, e: 0)
        def score(e):
            return module.score(e, s, build)-(.35 if len(candidates) > 1 and pending(s, e) >= e.hp else 0)
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
            self.modules = {slot: resilient_component(getattr(profile, slot), slot, self.registry) for slot in type(profile).model_fields}
            self.module_key = key
        modules = self.modules
        for module in modules.values():
            begin = getattr(module, "begin_frame", None)
            if begin: begin(s, build)
        target = self.target(s, profile, build)
        geometry_key = (s.room_visit_id, s.stats["can_fly"], str(s.layout["grid"]), str(s.layout["cells"]))
        if geometry_key != self.geometry_key:
            self.geometry, self.geometry_key = GeometryService(s), geometry_key
        g = self.geometry
        if modules["recovery"].reject_goal(s.frame, self.recovery_level, self.last_recovery_route):
            if self.last_route_goal: self.failed_goals.append(self.last_route_goal)
            self.last_recovery_route = s.frame
            self.route_failures += 1
        route = list(modules["position"].route(g, s, target.pos, build, modules["fire"], self.failed_goals).waypoints) if target else []
        weapon_capabilities = getattr(modules["fire"], "capabilities", None)
        if target and not route and weapon_capabilities and weapon_capabilities.break_plain_poop:
            # Normal tears can open an otherwise sealed lane through ordinary
            # poop. TNT, explosive/red poop and unknown variants are excluded.
            blockers = []
            for index, grid in (s.layout["grid"] or {}).items():
                if grid["type"] == 14 and grid["variant"] == 0 and grid["collision"] != 0:
                    position = Vec(x=grid["x"], y=grid["y"])
                    path = list(modules["position"].route(g, s, position, build, modules["fire"], terrain=True).waypoints)
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
            firing_velocity = velocity
            closest_waypoint = s.player.pos.distance(waypoint) if waypoint else 0
            clearance, wall = 1000., False
            early_clearance, first_contact, blocked_steps = 1000., 13, 0
            for tick in range(1, 13):
                velocity = velocity*.75+desired*.25
                new_position, velocity, blocked, unsafe = g.sweep(position, velocity)
                blocked_steps += blocked
                if unsafe: wall = True; break
                if tick == 1: firing_velocity = velocity
                position = new_position
                trajectory.append(position)
                if waypoint: closest_waypoint = min(closest_waypoint, position.distance(waypoint))
            gaps = threats.path_gaps(trajectory, s.player.collision_radius+extra)
            clearance = min(gaps, default=1000.)
            early_clearance = min(gaps[:3], default=1000.)
            first_contact = next((tick for tick, gap in enumerate(gaps, 1) if gap <= 0), 13)
            endpoint = (position.x, position.y)
            if endpoint not in routes: routes[endpoint] = threats.escape(g, position, build.move_speed, s.player.collision_radius+extra)
            escape, slack, exit_directions = routes[endpoint]
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
            for attack in modules["fire"].propose(s, build, profile, target, firing_velocity, g, modules["aim"]):
                shot, hit = attack.shoot, attack.hit_score
                postkill_safe = threats.attack_safe(target, build, trajectory, hit)
                safety = SafetyAssessment(clearance, slack, exit_directions, wall, postkill_safe)
                damage_risk = max(0, -clearance)
                utility = hit*6+stance+evade+modules["escape"].score(escape, slack)-(.04 if move != self.previous_move else 0)-blocked_steps*.02
                if not s.hostiles and move != (0, 0): utility -= .08
                if wall: utility -= 100
                candidates.append({"move": move, "shoot": shot, "safe": safety.safe,
                                   "clearance": clearance, "wall": wall, "hit": hit, "escape": escape,
                                   "escape_slack": slack, "escape_directions": exit_directions,
                                   "early_risk": max(0, -early_clearance), "first_contact": first_contact, "blocked_steps": blocked_steps,
                                   "utility": utility, "risk": damage_risk+(100 if not postkill_safe else 0), "fire_reason": attack.reason,
                                   "wait": attack.wait.__dict__ if attack.wait else None})
                if not postkill_safe:
                    candidates.append({**candidates[-1], "shoot": (0, 0), "hit": 0.,
                                       "safe": not wall and clearance > 0 and slack >= 6,
                                       "utility": utility-hit*6, "risk": damage_risk, "fire_reason": "death_effect_withdrawal"})
        # Safety is lexicographic. If every option is unsafe, minimize modelled
        # penetration before considering output; this is not a guarantee of life.
        best = max(candidates, key=lambda c: (c["safe"], not c["wall"], -c["early_risk"], c["escape_directions"] > 0,
                                             -c["risk"], c["first_contact"], c["utility"]))
        self.last_candidates = candidates
        component_failures = [failure for module in modules.values() if (failure := module.pop_failure())]
        self.previous_move = best["move"]
        self.last_risk = min(1., best["risk"]/30)
        return ActionFrame(frame=s.frame, room_visit_id=s.room_visit_id, move=best["move"], shoot=best["shoot"],
                           profile=name, target=target.instance_id if target else None,
                           state="RECOVER" if recovering else "EVADE" if best["clearance"] < 20 else "ENGAGE",
                           score={k: best[k] for k in ("clearance", "hit", "escape", "utility", "risk", "escape_slack", "early_risk", "first_contact")},
                           diagnostics={"route_goal": [round(route_goal.x), round(route_goal.y)] if route_goal else None,
                               "route_remaining": sum(a.distance(b) for a, b in zip([s.player.pos]+route, route)),
                               "no_safe_candidate": not any(c["safe"] for c in candidates), "fire_reason": best["fire_reason"], "wait": best["wait"],
                               "budget_exhausted": self.budget_exhausted, "evaluated_candidates": len(candidates),
                               "blocked_steps": best["blocked_steps"],
                               "component_failures": component_failures},
                           decision_ms=(time.perf_counter()-started)*1000)
