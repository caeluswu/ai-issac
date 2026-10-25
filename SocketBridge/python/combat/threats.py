"""Time-bounded threats, injectable enemy adapters and spatial broad phase."""
from collections import defaultdict, deque
from dataclasses import dataclass
import math
import numpy as np
from .models import Vec


@dataclass(frozen=True)
class SafetyAssessment:
    clearance: float
    escape_slack: float
    escape_directions: int
    wall: bool
    postkill_safe: bool = True
    @property
    def safe(self): return not self.wall and self.clearance > 0 and self.escape_slack >= 6 and self.postkill_safe


def distance_segment(px, py, ax, ay, bx, by):
    dx, dy = bx-ax, by-ay
    t = max(0., min(1., ((px-ax)*dx+(py-ay)*dy)/max(1e-9, dx*dx+dy*dy)))
    return math.hypot(px-ax-t*dx, py-ay-t*dy)


@dataclass(frozen=True)
class ThreatForecast:
    identity: str
    x: float
    y: float
    vx: float
    vy: float
    radius: float
    start: float = 0
    end: float = 45
    uncertainty: float = 0
    segment_end: tuple | None = None
    source: str = "observed"

    def gap(self, a, b, tick, player_radius, begin=None):
        begin = max(0, tick-1) if begin is None else begin
        if tick < self.start or begin > self.end: return 1000.
        if begin < self.start or tick > self.end:
            duration = max(1e-9, tick-begin)
            low, high = max(begin, self.start), min(tick, self.end)
            dx, dy = b[0]-a[0], b[1]-a[1]
            origin = a
            a = (origin[0]+dx*(low-begin)/duration, origin[1]+dy*(low-begin)/duration)
            b = (origin[0]+dx*(high-begin)/duration, origin[1]+dy*(high-begin)/duration)
            begin, tick = low, high
        if self.segment_end:
            ex, ey = self.segment_end
            # Segment/segment intersection prevents fast crossing between two
            # safe endpoints from passing through a thin laser unnoticed.
            dx, dy = b[0]-a[0], b[1]-a[1]
            lx, ly = ex-self.x, ey-self.y
            denominator = dx*ly-dy*lx
            if abs(denominator) > 1e-9:
                qx, qy = self.x-a[0], self.y-a[1]
                u, v = (qx*ly-qy*lx)/denominator, (qx*dy-qy*dx)/denominator
                if 0 <= u <= 1 and 0 <= v <= 1: return -self.radius-player_radius-self.uncertainty
            return min(distance_segment(*a, self.x, self.y, *self.segment_end),
                       distance_segment(*b, self.x, self.y, *self.segment_end))-self.radius-player_radius-self.uncertainty
        ax, ay = a[0]-self.x-self.vx*begin, a[1]-self.y-self.vy*begin
        bx, by = b[0]-self.x-self.vx*tick, b[1]-self.y-self.vy*tick
        return distance_segment(0, 0, ax, ay, bx, by)-self.radius-player_radius-self.uncertainty


class GenericEnemy:
    """Unknown phases remain unknown; current velocity has bounded error."""
    death_effect_radius = 0
    def attack_allowed(self, enemy, snapshot): return True
    def forecast(self, enemy, snapshot):
        if enemy.collision_class == 0: return []
        return [ThreatForecast(enemy.instance_id, enemy.pos.x, enemy.pos.y, enemy.vel.x, enemy.vel.y,
                               enemy.collision_radius, uncertainty=4+min(8, enemy.vel.length()*2),
                               source="observed_motion_approximation")]
    def death_radius(self, enemy): return self.death_effect_radius


class ExplosiveDeath(GenericEnemy):
    """Normal Boom Fly only; 100px is a conservative uncalibrated model bound."""
    death_effect_radius = 100


class Pursuer(GenericEnemy):
    def forecast(self, enemy, snapshot):
        predictions = super().forecast(enemy, snapshot)
        if enemy.collision_class == 0: return predictions
        direction = (snapshot.player.pos-enemy.pos).unit()
        speed = max(1.5, enemy.vel.length())
        predictions.append(ThreatForecast(enemy.instance_id+":turn", enemy.pos.x, enemy.pos.y,
            direction.x*speed, direction.y*speed, enemy.collision_radius, uncertainty=8,
            source="pursuit_hypothesis_uncalibrated"))
        return predictions


class ContactExplosive(ExplosiveDeath):
    def forecast(self, enemy, snapshot):
        # Captured full-health Mullibooms self-destructed without our firing.
        # Post-kill checks alone cannot protect a route from proximity blasts.
        motion = Pursuer().forecast(enemy, snapshot)
        return motion+[ThreatForecast(t.identity+":contact_blast", t.x, t.y, t.vx, t.vy, 100,
            uncertainty=max(8, t.uncertainty), source="possible_contact_triggered_explosion") for t in motion]


class BurstMover(Pursuer):
    def forecast(self, enemy, snapshot):
        return [ThreatForecast(t.identity, t.x, t.y, t.vx, t.vy, t.radius, t.start, t.end,
                               max(t.uncertainty, 18+enemy.vel.length()*3), source="burst_turn_bound_uncalibrated")
                for t in super().forecast(enemy, snapshot)]


class LandingMover(BurstMover):
    def forecast(self, enemy, snapshot):
        # Ground collision being disabled during a jump is not evidence that
        # its future landing is harmless. State numbers are deliberately not
        # decoded without a validated phase table.
        grounded = enemy.model_copy(update={"collision_class": 4})
        predictions = super().forecast(grounded, snapshot)
        landing = enemy.pos+enemy.vel*6
        predictions.append(ThreatForecast(enemy.instance_id+":landing", landing.x, landing.y, 0, 0,
            enemy.collision_radius, uncertainty=24, source="uncalibrated_landing_envelope"))
        return predictions


class ExplosiveBurst(LandingMover):
    # Ticking Spider: observed airborne class zero must retain landing threats.
    # Unhurt line-of-sight death at ~72px, hurt at ~54-60px, ordinary variant.
    # 70px plus player radius and withdrawal reserve remains an engineering
    # bound, not the engine's exact radius.
    death_effect_radius = 70


class EnemyAdapters:
    def __init__(self):
        self.default = GenericEnemy()
        # Type 16 variant 2 has captured same-update explosion damage and death.
        # Use the conservative 100px envelope until its radius is calibrated.
        self.adapters = {(25, 0): ExplosiveDeath(), (16, 2): ContactExplosive(), (10, 0): Pursuer(), (18, 0): Pursuer(),
                         (85, 0): BurstMover(), (94, 0): BurstMover(), (250, 0): ExplosiveBurst(),
                         (305, 0): BurstMover(), (29, 0): LandingMover()}
    def resolve(self, enemy): return self.adapters.get((enemy.type, enemy.variant), self.default)
    def register(self, kind, variant, adapter): self.adapters[kind, variant] = adapter


class ThreatField:
    def __init__(self, s, adapters=None):
        self.snapshot = s
        self.adapters = adapters or EnemyAdapters()
        self.threats = []
        for enemy in s.hostiles: self.threats.extend(self.adapters.resolve(enemy).forecast(enemy, s))
        for enemy in s.hostiles:
            radius = self.adapters.resolve(enemy).death_radius(enemy)
            if radius and enemy.hp <= float(s.stats.get("damage", 3.5)) and s.projectiles["player_tears"]:
                self.threats.append(ThreatForecast(enemy.instance_id+":imminent_death", enemy.pos.x, enemy.pos.y,
                    enemy.vel.x, enemy.vel.y, radius, uncertainty=8, source="possible_inflight_lethal_hit"))
        for i, p in enumerate(s.projectiles["enemy_projectiles"] or []):
            self.threats.append(ThreatForecast("bullet:"+str(p.get("instance_id", i)),
                p["pos"]["x"], p["pos"]["y"], p["vel"]["x"], p["vel"]["y"], p["collision_radius"], uncertainty=5))
        for b in s.bombs:
            known = b.get("timer_known", False)
            self.threats.append(ThreatForecast("bomb:"+str(b.get("id", 0)), b["pos"]["x"], b["pos"]["y"],
                b.get("vel", {}).get("x", 0), b.get("vel", {}).get("y", 0), b["explosion_radius"],
                start=max(0, b["timer"]-3) if known else 0, end=max(0, b["timer"])+6 if known else 45,
                uncertainty=4, source="known_fuse" if known else "unknown_fuse_full_horizon"))
        # A live explosion can propagate through TNT. Idle TNT is a solid
        # obstacle, not a permanent blast field. Destroyed cells retain a short
        # transition field assembled from consecutive raw observations.
        frontier = [(b["pos"]["x"], b["pos"]["y"], b["explosion_radius"]) for b in s.bombs
                    if not b.get("timer_known", False) or b["timer"] <= 45]
        residuals = getattr(s, "environment_residuals", [])
        frontier += [(h["x"], h["y"], 100) for h in residuals]
        for h in residuals:
            self.threats.append(ThreatForecast("tnt_residual:"+str(h["id"]), h["x"], h["y"], 0, 0, 100,
                end=max(0, h["expires_frame"]-s.frame), uncertainty=8, source="observed_tnt_transition"))
        remaining = {i: c for i, c in (s.layout["grid"] or {}).items() if c["type"] == 12 and c["collision"] != 0}
        for x, y, radius in frontier:
            for index, cell in list(remaining.items()):
                if math.hypot(cell["x"]-x, cell["y"]-y) > radius+20: continue
                del remaining[index]
                frontier.append((cell["x"], cell["y"], 100))
                self.threats.append(ThreatForecast("tnt_chain:"+str(index), cell["x"], cell["y"], 0, 0, 100,
                    uncertainty=8, source="potential_tnt_chain"))
        for i, laser in enumerate(s.projectiles["lasers"] or []):
            if not laser["is_enemy"]: continue
            angle = math.radians(laser["angle"])
            end = (laser["pos"]["x"]+math.cos(angle)*laser["max_distance"],
                   laser["pos"]["y"]+math.sin(angle)*laser["max_distance"])
            self.threats.append(ThreatForecast("laser:"+str(i), laser["pos"]["x"], laser["pos"]["y"],
                0, 0, laser.get("collision_radius", 10), segment_end=end, uncertainty=4))
        for i, hazard in enumerate(s.hazards):
            if hazard.get("is_extinguished", False): continue
            self.threats.append(ThreatForecast("hazard:"+str(i), hazard["pos"]["x"], hazard["pos"]["y"], 0, 0,
                hazard["collision_radius"], end=min(45, max(1, hazard.get("remaining_ticks", 45))), uncertainty=2,
                source=hazard.get("mechanism", "observed_hazard")))
        # Retained bridge death events survive partition removal. Conditional
        # prediction becomes a bounded residual field; disappearing alone does not.
        for death in getattr(s, "recent_deaths", []) or []:
            adapter = self.adapters.adapters.get((death.get("type"), death.get("variant")), self.adapters.default)
            radius = adapter.death_effect_radius
            if radius and s.frame-death["frame"] <= 6:
                self.threats.append(ThreatForecast("death:"+death["instance_id"], death["pos"]["x"], death["pos"]["y"],
                    0, 0, radius, end=6-(s.frame-death["frame"]), uncertainty=8, source="confirmed_death_approximate_radius"))
        self.buckets = defaultdict(set)
        self.dynamic = []
        self.static = []
        self.indexed_bins = set()
        self.dense = len(self.threats) >= 128
        self.segments = [t for t in self.threats if t.segment_end]
        circles = [(t.x, t.y, t.vx, t.vy, t.radius+t.uncertainty, t.start, t.end) for t in self.threats if not t.segment_end]
        self.circle_data = np.asarray(circles, dtype=float).reshape(-1, 7) if self.dense else None
        if self.circle_data is not None: self.circle_data.setflags(write=False)
        self.static_ready = not self.dense
        for i, t in enumerate(self.threats):
            if t.vx or t.vy: self.dynamic.append(i)
            else:
                self.static.append(i)
                if self.static_ready: self.index_threat(i, -1)

    def circle_gaps(self, a, b, tick, radius, begin=None):
        begin = max(0, tick-1) if begin is None else begin
        return self.circle_gaps_arrays(a[0], a[1], b[0], b[1], tick, radius, begin)

    def circle_gaps_arrays(self, ax, ay, bx, by, tick, radius, begin):
        data = self.circle_data
        if data is None or not len(data): return None
        active = (tick >= data[:, 5]) & (begin <= data[:, 6])
        low, high = np.maximum(begin, data[:, 5]), np.minimum(tick, data[:, 6])
        duration = np.maximum(1e-9, tick-begin)
        startx, starty, deltax, deltay = ax, ay, bx-ax, by-ay
        ax = startx+deltax*(low-begin)/duration-data[:, 0]-data[:, 2]*low
        ay = starty+deltay*(low-begin)/duration-data[:, 1]-data[:, 3]*low
        bx = startx+deltax*(high-begin)/duration-data[:, 0]-data[:, 2]*high
        by = starty+deltay*(high-begin)/duration-data[:, 1]-data[:, 3]*high
        dx, dy = bx-ax, by-ay
        phase = np.clip(-(ax*dx+ay*dy)/np.maximum(1e-9, dx*dx+dy*dy), 0, 1)
        return np.where(active, np.hypot(ax+phase*dx, ay+phase*dy)-data[:, 4]-radius, 1000.)

    def path_gaps(self, trajectory, radius):
        if len(trajectory) < 2: return []
        if not self.dense:
            return [self.gap((a.x, a.y), (b.x, b.y), tick, radius)
                    for tick, (a, b) in enumerate(zip(trajectory, trajectory[1:]), 1)]
        p = np.asarray([(v.x, v.y) for v in trajectory], dtype=float)
        ticks = np.arange(1, len(p), dtype=float)[:, None]
        gaps = self.circle_gaps_arrays(p[:-1, 0, None], p[:-1, 1, None], p[1:, 0, None], p[1:, 1, None], ticks, radius, ticks-1)
        result = np.min(gaps, axis=1) if gaps is not None else np.full(len(p)-1, 1000.)
        if self.segments:
            for index in range(len(result)):
                result[index] = min(result[index], *(t.gap(p[index], p[index+1], index+1, radius) for t in self.segments))
        return result.tolist()

    def index_threat(self, i, step):
        t = self.threats[i]
        begin, end = (max(0, step*6-1), min(45, (step+1)*6)) if step >= 0 else (0, 0)
        xs, ys = [t.x+t.vx*begin, t.x+t.vx*end], [t.y+t.vy*begin, t.y+t.vy*end]
        if t.segment_end: xs.append(t.segment_end[0]); ys.append(t.segment_end[1])
        r = t.radius+t.uncertainty
        for x in range(math.floor((min(xs)-r)/64), math.floor((max(xs)+r)/64)+1):
            for y in range(math.floor((min(ys)-r)/64), math.floor((max(ys)+r)/64)+1): self.buckets[step, x, y].add(i)

    def query(self, a, b, radius, tick, begin=None):
        indices = set()
        if not self.static_ready:
            for i in self.static: self.index_threat(i, -1)
            self.static_ready = True
        bins = range(min(7, int((tick if begin is None else begin)//6)), min(7, int(tick//6))+1)
        for step in bins:
            if step not in self.indexed_bins:
                for i in self.dynamic: self.index_threat(i, step)
                self.indexed_bins.add(step)
        for x in range(math.floor((min(a[0], b[0])-radius)/64), math.floor((max(a[0], b[0])+radius)/64)+1):
            for y in range(math.floor((min(a[1], b[1])-radius)/64), math.floor((max(a[1], b[1])+radius)/64)+1):
                indices.update(self.buckets.get((-1, x, y), ()))
                for step in bins:
                    indices.update(self.buckets.get((step, x, y), ()))
        return (self.threats[i] for i in indices)

    def gap(self, a, b, tick, radius):
        if self.dense:
            gaps = self.circle_gaps(a, b, tick, radius)
            values = ([float(np.min(gaps))] if gaps is not None else [])+[t.gap(a, b, tick, radius) for t in self.segments]
            return min(values, default=1000.)
        return min((t.gap(a, b, tick, radius) for t in self.query(a, b, radius, tick)), default=1000.)

    def collides(self, a, b, tick, radius, begin=None):
        if self.dense:
            gaps = self.circle_gaps(a, b, tick, radius, begin)
            return bool(gaps is not None and np.any(gaps <= 0)) or any(t.gap(a, b, tick, radius, begin) <= 0 for t in self.segments)
        return any(t.gap(a, b, tick, radius, begin) <= 0 for t in self.query(a, b, radius, tick, begin))

    def edge_safe(self, a, b, begin, end, radius):
        steps = max(1, math.ceil((end-begin)/3))
        for step in range(steps):
            low, high = step/steps, (step+1)/steps
            first = (a.x+(b.x-a.x)*low, a.y+(b.y-a.y)*low)
            last = (a.x+(b.x-a.x)*high, a.y+(b.y-a.y)*high)
            if self.collides(first, last, begin+(end-begin)*high, radius, begin+(end-begin)*low): return False
        return not self.collides((b.x, b.y), (b.x, b.y), end+6, radius, end)

    def escape(self, geometry, position, speed, radius, start_tick=12):
        origin = geometry.cell(position)
        if origin not in geometry.walkable: return 0., -45., 0
        if not self.threats: return 1., 45., 2
        point = (position.x, position.y)
        if self.collides(point, point, start_tick, radius, start_tick): return 0., -45., 0
        # Time-expanded reachability: each traversed edge is checked at its
        # actual arrival time, including the six-update withdrawal reserve.
        queue, seen, directions = deque([(origin, start_tick, None)]), {origin}, set()
        first = geometry.positions[origin]
        best_slack = -45.
        while queue and len(seen) <= 48:
            cell, tick, direction = queue.popleft()
            for neighbor in geometry.neighbors.get(cell, []):
                if neighbor in seen: continue
                a, b = position if cell == origin else geometry.positions[cell], geometry.positions[neighbor]
                if cell == origin and not geometry.line_clear(a, b, radius=geometry.physical_radius): continue
                arrival = tick+a.distance(b)/max(1., speed)
                if arrival+6 > 45: continue
                valid = self.edge_safe(a, b, tick, arrival, radius)
                if not valid: continue
                seen.add(neighbor)
                branch = direction if direction is not None else neighbor
                if b.distance(first) >= 60:
                    point = (b.x, b.y)
                    ttc = next((t for t in range(math.ceil(arrival), 46, 3) if self.collides(point, point, t, radius)), 46)
                    slack = ttc-arrival
                    if slack >= 6:
                        directions.add(branch)
                        best_slack = max(best_slack, slack)
                queue.append((neighbor, arrival, branch))
        count = len(directions)
        fraction = min(1., count/2)
        # Reserve is measured along an actual escape path, not by pretending
        # the player holds still at the current rollout endpoint forever.
        return fraction, best_slack, count

    def attack_safe(self, target, build, positions, hit, footprint=None):
        if target is None or hit <= 0: return True
        affected = [target]
        if footprint:
            ax, ay, bx, by, width = footprint
            for enemy in self.snapshot.hostiles:
                adapter = self.adapters.resolve(enemy)
                radius = adapter.death_radius(enemy)
                if (radius or not adapter.attack_allowed(enemy, self.snapshot)) and enemy.instance_id != target.instance_id and distance_segment(enemy.pos.x, enemy.pos.y, ax, ay, bx, by) <= enemy.collision_radius+width+enemy.vel.length()*12:
                    affected.append(enemy)
        # Keep the blast reserve throughout an attack sequence. Waiting until
        # the final hit is too late when earlier tears are already in flight.
        for enemy in affected:
            adapter = self.adapters.resolve(enemy)
            if not adapter.attack_allowed(enemy, self.snapshot): return False
            radius = adapter.death_radius(enemy)
            if not radius: continue
            flight = max(1., enemy.pos.distance(self.snapshot.player.pos)/build.tear_speed)
            point = positions[min(len(positions)-1, round(flight))]
            predicted = enemy.pos+enemy.vel*min(flight, 12)
            if min(point.distance(enemy.pos), point.distance(predicted)) <= radius+self.snapshot.player.collision_radius+8: return False
        return True
