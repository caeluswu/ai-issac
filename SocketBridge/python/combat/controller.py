"""Registered component execution and a 45-candidate receding-horizon solver."""
from collections import deque
from dataclasses import dataclass
import math
import statistics
import time
from .models import Vec, ZERO, BuildProfile, ActionFrame, Enemy
from .geometry import GeometryService, segment_distance
from .components import EXECUTABLES, resilient_component, AttackProposal
from .threats import ThreatField, EnemyAdapters, SafetyAssessment
from .motion import PlayerMotion

MOVES = tuple((x, y) for y in (-1, 0, 1) for x in (-1, 0, 1))
SHOTS = ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))


def select_joint_action(candidates, uncertainty_px=8):
    """Safe actions first; permit escape within an uncertain penetration band.

    Predicted overlap is a distance approximation, not damage probability.
    A tiny difference between two already unsafe paths cannot outweigh an
    actual withdrawal route. A contact-free near future still takes priority.
    """
    feasible = [c for c in candidates if not c["wall"]] or candidates
    minimum = min(c["early_risk"] for c in feasible)
    return max(candidates, key=lambda c: (
        c["safe"], not c["wall"], c["early_risk"] == 0,
        c["early_risk"] <= minimum+uncertainty_px,
        c["escape_directions"] > 0, -c["risk"], -c["exposure"], c["first_contact"], c["utility"]))


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
        p.player_displacement_scale = 2.
        p.move_speed = max(1, float(s.stats["speed"])*4.4)*p.player_displacement_scale
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
        self.emergency_until = -1
        self.emergency_profile = None

    def select_profile(self, s, plan, build):
        if s.frame < self.emergency_until or s.hp <= 2:
            if build.family != "normal_tears": raise ValueError("UNSUPPORTED_EMERGENCY_PROFILE")
            if self.emergency_profile is None:
                from .catalog import default_plan, compile_plan
                emergency = default_plan(plan.context_ref)
                p = emergency.profiles[emergency.default_profile]
                p.target.component = "target.finish_first"
                p.target.params.update(min_hold_ticks=0, switch_margin=.05)
                p.position.params.update(min_range_ratio=.6, max_range_ratio=.9)
                p.fire.params.update(min_hit_score=.1)
                p.evade.params.update(extra_margin_px=8)
                p.escape.params.update(horizon_ticks=45)
                self.emergency_profile = compile_plan(emergency, plan.context_ref).profiles[emergency.default_profile]
            if self.profile_name != "contract_emergency":
                self.profile_name, self.profile_since = "contract_emergency", s.frame
                self.positions.clear()
            return self.profile_name, self.emergency_profile
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

    def solve(self, s, plan, build, movement_only=False, navigation_goal=None, allowed_devices=()):
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
        target = None if movement_only else self.target(s, profile, build)
        fire_cells = tuple(sorted((h["id"], h["pos"]["x"], h["pos"]["y"]) for h in s.hazards
                                  if h.get("type") == "FIREPLACE" and not h.get("is_extinguished", False)))
        geometry_key = (s.room_visit_id, s.stats["can_fly"], str(s.layout["grid"]), str(s.layout["cells"]), tuple(allowed_devices), fire_cells)
        if geometry_key != self.geometry_key:
            self.geometry, self.geometry_key = GeometryService(s, allowed_devices), geometry_key
        g = self.geometry
        if modules["recovery"].reject_goal(s.frame, self.recovery_level, self.last_recovery_route):
            if self.last_route_goal: self.failed_goals.append(self.last_route_goal)
            self.last_recovery_route = s.frame
            self.route_failures += 1
        route = list(modules["position"].route(g, s, target.pos, build, modules["fire"], self.failed_goals).waypoints) if target else []
        if navigation_goal is not None:
            route = g.path(s.player.pos, navigation_goal)+[navigation_goal]
            # The start grid center is an A* bookkeeping node. Requiring a
            # return to it creates a two-cell oscillation near cell bisectors.
            if len(route) > 1 and g.line_clear(s.player.pos, route[1], radius=g.physical_radius): route.pop(0)
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
        repositioning = False
        if target and not route and not movement_only:
            relocate = getattr(modules["position"], "reposition", None)
            if relocate:
                route = list(relocate(g, s, target.pos, build, self.failed_goals).waypoints)
                repositioning = bool(route)
        # Route following supplies long-horizon progress to the short local
        # rollout. Without it a safe local optimum can strand the controller.
        route_goal = route[-1] if route else None
        self.last_route_goal = (round(route_goal.x), round(route_goal.y)) if route_goal else None
        while route and route[0].distance(s.player.pos) < (18 if len(route) > 1 else 5): route.pop(0)
        waypoint = route[0] if route else None
        suffix = [0.]*len(route)
        for i in range(len(route)-2, -1, -1): suffix[i] = suffix[i+1]+route[i].distance(route[i+1])
        initial_route_cost = s.player.pos.distance(route[0])+suffix[0] if route else 0.
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
            desired = PlayerMotion.desired(move, build)
            position, velocity = s.player.pos, s.player.vel
            trajectory = [position]
            future_index, discounted_progress = 0, 0.
            firing_velocity = velocity
            closest_waypoint = s.player.pos.distance(waypoint) if waypoint else 0
            clearance, wall = 1000., False
            early_clearance, first_contact, blocked_steps = 1000., 13, 0
            for tick in range(1, 13):
                rollout_desired = desired
                if movement_only and navigation_goal is not None and tick > 1:
                    if future_index < len(route):
                        offset = route[future_index]-position
                        direction = (0 if abs(offset.x) < 7 else (1 if offset.x > 0 else -1),
                                     0 if abs(offset.y) < 7 else (1 if offset.y > 0 else -1))
                        rollout_desired = PlayerMotion.desired(direction, build)
                        if future_index == len(route)-1:
                            rollout_desired = rollout_desired*min(1., offset.length()/max(1., 3*build.move_speed))
                    else: rollout_desired = ZERO
                new_position, velocity, blocked, unsafe = PlayerMotion.step(position, velocity, rollout_desired, g, build)
                blocked_steps += blocked
                if unsafe: wall = True; break
                if tick == 1: firing_velocity = velocity
                position = new_position
                trajectory.append(position)
                if movement_only and navigation_goal is not None and route:
                    while future_index < len(route) and position.distance(route[future_index]) < (18 if future_index < len(route)-1 else 5): future_index += 1
                    remaining = position.distance(route[future_index])+suffix[future_index] if future_index < len(route) else 0.
                    discounted_progress += (initial_route_cost-remaining)*(.85**(tick-1))
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
                stance = modules["position"].score(delta, desired*build.player_displacement_scale, s, build)
                if waypoint:
                    # Receding-horizon actions may brake or turn at the next
                    # update. Constant-input endpoint scoring penalizes crossing
                    # a nearby waypoint and makes zero input a local optimum.
                    stance += (s.player.pos.distance(waypoint)-closest_waypoint)*(.35 if recovering else .15)
            evade = modules["evade"].score(clearance, desired*build.player_displacement_scale)
            attacks = ([AttackProposal((0, 0), 0, 0, "entry_move_only")] if movement_only else
                       modules["fire"].propose(s, build, profile, target, firing_velocity, g, modules["aim"]))
            for attack in attacks:
                shot, hit = attack.shoot, attack.hit_score
                attack_target = next((e for e in s.hostiles if e.instance_id == attack.target_id), target)
                postkill_safe = threats.attack_safe(attack_target, build, trajectory, hit, attack.footprint)
                safety = SafetyAssessment(clearance, slack, exit_directions, wall, postkill_safe)
                damage_risk = max(0, -clearance)
                utility = hit*6+stance+evade+modules["escape"].score(escape, slack)-(.04 if move != self.previous_move else 0)-blocked_steps*.02
                if movement_only: utility = min(100, clearance)/20+escape+max(0, slack)/45-blocked_steps*.05
                if movement_only and waypoint:
                    # Score the complete path, with earlier progress worth more.
                    # A lucky near-pass of one waypoint must not make waiting
                    # preferable to a physically feasible first action.
                    utility += discounted_progress*.15
                if not s.hostiles and move != (0, 0): utility -= .08
                if wall: utility -= 100
                candidates.append({"move": move, "shoot": shot, "safe": safety.safe,
                                   "clearance": clearance, "wall": wall, "hit": hit, "escape": escape,
                                   "escape_slack": slack, "escape_directions": exit_directions,
                                   "early_risk": max(0, -early_clearance), "first_contact": first_contact, "blocked_steps": blocked_steps,
                                   "exposure": sum(max(0, -gap) for gap in gaps),
                                   "first_route_progress": (s.player.pos.distance(waypoint)-trajectory[1].distance(waypoint)) if waypoint and len(trajectory) > 1 else 0.,
                                   "utility": utility, "risk": damage_risk+(100 if not postkill_safe else 0), "fire_reason": attack.reason,
                                   "attack_target": attack_target.instance_id if attack_target else None,
                                   "wait": attack.wait.__dict__ if attack.wait else None})
                if not postkill_safe:
                    candidates.append({**candidates[-1], "shoot": (0, 0), "hit": 0.,
                                       "safe": not wall and clearance > 0 and slack >= 6,
                                       "utility": utility-hit*6, "risk": damage_risk, "fire_reason": "attack_safety_withdrawal"})
        # Safety is lexicographic. If every option is unsafe, minimize modelled
        # penetration before considering output; this is not a guarantee of life.
        best = select_joint_action(candidates)
        forward_override = False
        if movement_only and navigation_goal is not None and initial_route_cost > 12 and s.player.vel.length() < .5 and best["move"] == (0, 0):
            forward = [c for c in candidates if c["safe"] and c["move"] != (0, 0) and c["first_route_progress"] > .25]
            if forward:
                best = select_joint_action(forward)
                forward_override = True
        self.last_candidates = candidates
        component_failures = [failure for module in modules.values() if (failure := module.pop_failure())]
        self.previous_move = best["move"]
        self.last_risk = min(1., best["risk"]/30)
        return ActionFrame(frame=s.frame, room_visit_id=s.room_visit_id, move=best["move"], shoot=best["shoot"],
                           profile=name, target=best["attack_target"],
                           state="ENTRY_GUARD" if movement_only else "RECOVER" if recovering else "EVADE" if best["clearance"] < 20 else "ENGAGE",
                           score={k: best[k] for k in ("clearance", "hit", "escape", "utility", "risk", "escape_slack", "early_risk", "first_contact")},
                           diagnostics={"route_goal": [round(route_goal.x), round(route_goal.y)] if route_goal else None,
                               "movement_target": target.instance_id if target else None,
                               "route_remaining": sum(a.distance(b) for a, b in zip([s.player.pos]+route, route)),
                               "no_safe_candidate": not any(c["safe"] for c in candidates), "fire_reason": best["fire_reason"], "wait": best["wait"],
                               "budget_exhausted": self.budget_exhausted, "evaluated_candidates": len(candidates),
                               "blocked_steps": best["blocked_steps"],
                               "near_risk_tolerance_px": 8,
                               "occlusion_reposition": repositioning,
                               "navigation_forward_override": forward_override, "first_route_progress": best["first_route_progress"],
                               "component_failures": component_failures},
                           decision_ms=(time.perf_counter()-started)*1000)
