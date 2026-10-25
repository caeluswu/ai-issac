"""Registered component execution and a 45-candidate receding-horizon solver."""
from collections import deque
from dataclasses import dataclass
import math
import statistics
import time
from .models import Vec, ZERO, BuildProfile, ActionFrame, Enemy
from .geometry import GeometryService, segment_distance

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
    def __init__(self):
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
        def score(e):
            distance = e.pos.distance(s.player.pos)
            threat = min(1., (e.vel.length()+1)*30/max(30, distance))
            finish = 1-min(1., e.hp/max(1, e.max_hp))
            approach = 1-min(1., distance/max(1, build.effective_range))
            if profile.target.component.endswith("threat_first"): return .8*threat+.2*approach
            if profile.target.component.endswith("finish_first"): return .8*finish+.2*approach
            return p["threat_weight"]*threat+p["finish_weight"]*finish+p["approach_cost_weight"]*approach
        best = max(candidates, key=lambda e: (score(e), e.instance_id))
        old = next((e for e in candidates if e.instance_id == self.target_id), None)
        if old and (s.frame-self.target_since < p["min_hold_ticks"] or score(best) < score(old)+p["switch_margin"]): best = old
        if best.instance_id != self.target_id: self.target_id, self.target_since = best.instance_id, s.frame
        return best

    def hit_score(self, s, build, profile, target, shot, velocity, geometry):
        if not target or shot == (0, 0) or not target.vulnerable: return 0.
        launch = Vec(x=shot[0], y=shot[1])*build.tear_speed
        if profile.aim.params["movement_inheritance"]:
            measured = s.movement_inheritance.get(f"{shot[0]},{shot[1]}")
            # Candidate movement affects launch velocity; the measured current
            # inheritance anchors the approximation rather than hiding uncertainty.
            launch += (Vec(**measured)+(velocity-s.player.vel)*.5) if measured else velocity*.5
        enemy_velocity = target.vel if profile.aim.component == "aim.predictive" else ZERO
        relative = launch-enemy_velocity
        delta = target.pos-s.player.pos
        ticks = delta.dot(relative)/max(1e-9, relative.dot(relative))
        # max_lead_ticks bounds the *enemy forecast*, not tear lifetime. A
        # distant stationary target remains shootable even with a short lead.
        forecast_ticks = min(max(0, ticks), profile.aim.params["max_lead_ticks"])
        if ticks > forecast_ticks and enemy_velocity.length() > 0:
            delta = target.pos+enemy_velocity*forecast_ticks-s.player.pos
            ticks = delta.dot(launch)/max(1e-9, launch.dot(launch))
        if ticks < 0 or ticks*launch.length() > build.effective_range: return 0.
        hit = s.player.pos+launch*ticks
        miss = hit.distance(target.pos+enemy_velocity*min(ticks, profile.aim.params["max_lead_ticks"]))
        miss += enemy_velocity.length()*max(0, ticks-forecast_ticks)*.2
        margin = target.collision_radius+5
        end = hit-(hit-s.player.pos).unit()*22 if target.instance_id.startswith("terrain:") else hit
        if not geometry.line_clear(s.player.pos, end, shot=True): return 0.
        return max(0., 1-miss/(margin+8))*(1 if build.calibrated else .8)

    def solve(self, s, plan, build):
        started = time.perf_counter()
        name, profile = self.select_profile(s, plan, build)
        target = self.target(s, profile, build)
        g = GeometryService(s)
        route = g.firing_route(s.player.pos, target.pos, build.effective_range) if target else []
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
        while route and route[0].distance(s.player.pos) < 18: route.pop(0)
        waypoint = route[0] if route else None
        predictions = MotionPredictor().forecast(s)
        extra = profile.evade.params["extra_margin_px"]
        self.positions.append((s.frame, s.player.pos))
        history = [p for f, p in self.positions if f <= s.frame-profile.recovery.params["stall_window_ticks"]]
        if history and self.previous_move != (0, 0) and s.player.pos.distance(history[-1]) < 8 and s.frame > self.recover_until:
            self.recover_until = s.frame+profile.recovery.params["recovery_ticks"]
            self.recoveries.append(s.frame); self.positions.clear()
        recovering = s.frame < self.recover_until
        horizon = profile.escape.params["horizon_ticks"]
        safe = g.future_safe_cells(s.hostiles, horizon)
        safe_steps = max(1, round(horizon*build.move_speed/40))
        routes = {}
        candidates = []
        for move in MOVES:
            desired = Vec(x=move[0], y=move[1]).unit()*build.move_speed
            position, velocity = s.player.pos, s.player.vel
            clearance, wall = 1000., False
            for tick in range(1, 13):
                velocity = velocity*.75+desired*.25
                new_position = position+velocity
                if not g.line_clear(position, new_position): wall = True; break
                for e in predictions:
                    # Relative swept segment prevents tunnelling across fast entities.
                    a, b = position-(e.start+e.velocity*(tick-1)), new_position-(e.start+e.velocity*tick)
                    gap = segment_distance(ZERO, a, b)-s.player.collision_radius-e.radius-e.uncertainty-extra
                    clearance = min(clearance, gap)
                for bullet in s.projectiles["enemy_projectiles"] or []:
                    bp, bv = Vec(**bullet["pos"]), Vec(**bullet["vel"])
                    clearance = min(clearance, segment_distance(ZERO, position-(bp+bv*(tick-1)), new_position-(bp+bv*tick))
                                    -s.player.collision_radius-bullet["collision_radius"]-3-extra)
                for bomb in s.bombs:
                    if bomb["timer"] <= tick+3:
                        clearance = min(clearance, new_position.distance(Vec(**bomb["pos"]))-bomb["explosion_radius"]-s.player.collision_radius-4)
                for laser in s.projectiles["lasers"] or []:
                    if laser["is_enemy"]:
                        a = Vec(**laser["pos"]); angle = math.radians(laser["angle"])
                        b = a+Vec(x=math.cos(angle), y=math.sin(angle))*laser["max_distance"]
                        clearance = min(clearance, segment_distance(new_position, a, b)-s.player.collision_radius-laser.get("collision_radius", 10)-4)
                for hazard in s.hazards:
                    if not hazard.get("is_extinguished", False):
                        clearance = min(clearance, new_position.distance(Vec(**hazard["pos"]))-s.player.collision_radius-hazard["collision_radius"]-2)
                position = new_position
            cell = g.cell(position)
            if cell not in routes: routes[cell] = g.reachable_fraction(position, safe, safe_steps)
            escape = routes[cell]
            stance = 0.
            if target:
                delta = target.pos-position
                distance = delta.length()
                low, high = (profile.position.params[x]*build.effective_range for x in ("min_range_ratio", "max_range_ratio"))
                stance = -max(low-distance, 0, distance-high)/50
                # Seek one of the four firing lanes as well as distance.
                stance -= min(abs(delta.x), abs(delta.y))/90
                if profile.position.component == "position.kite": stance += min(distance, low)/150
                if profile.position.component == "position.strafe":
                    side = profile.position.params["preferred_side"]
                    sign = -1 if side == "left" else 1
                    if side == "auto": sign = 1 if (s.frame//profile.position.params["side_switch_ticks"]) % 2 else -1
                    stance += sign*(desired.x*delta.y-desired.y*delta.x)/max(1, distance*build.move_speed)*.5
                if recovering:
                    path = g.path(s.player.pos, target.pos)
                    if len(path) > 1: stance += (path[1]-s.player.pos).unit().dot(desired)/max(1, build.move_speed)
                    if move == self.previous_move: stance -= 2
                if waypoint:
                    stance += (s.player.pos.distance(waypoint)-position.distance(waypoint))*.12
            evade = min(100, clearance)/150 if profile.evade.component == "evade.max_clearance" else -desired.length()*.04
            for shot in SHOTS:
                hit = self.hit_score(s, build, profile, target, shot, desired, g)
                if shot != (0, 0) and hit < profile.fire.params["min_hit_score"]: continue
                damage_risk = max(0, -clearance)
                utility = hit*3+stance+evade+escape*1.5-(.08 if move != self.previous_move else 0)
                if escape < profile.escape.params["min_safe_area_ratio"]: utility -= 2
                if wall: utility -= 100
                candidates.append({"move": move, "shoot": shot, "safe": not wall and clearance > 0,
                                   "clearance": clearance, "wall": wall, "hit": hit, "escape": escape,
                                   "utility": utility, "risk": damage_risk})
        # Safety is lexicographic. If every option is unsafe, minimize modelled
        # penetration before considering output; this is not a guarantee of life.
        best = max(candidates, key=lambda c: (c["safe"], not c["wall"], -c["risk"], c["utility"]))
        self.last_candidates = candidates
        self.previous_move = best["move"]
        self.last_risk = min(1., best["risk"]/30)
        return ActionFrame(frame=s.frame, room_visit_id=s.room_visit_id, move=best["move"], shoot=best["shoot"],
                           profile=name, target=target.instance_id if target else None,
                           state="RECOVER" if recovering else "EVADE" if best["clearance"] < 20 else "ENGAGE",
                           score={k: best[k] for k in ("clearance", "hit", "escape", "utility", "risk")},
                           decision_ms=(time.perf_counter()-started)*1000)
