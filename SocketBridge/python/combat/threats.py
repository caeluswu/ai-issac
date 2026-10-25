"""Time-bounded threats, injectable enemy adapters and spatial broad phase."""
from collections import defaultdict, deque
from dataclasses import dataclass
import math
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


class BurstMover(Pursuer):
    def forecast(self, enemy, snapshot):
        return [ThreatForecast(t.identity, t.x, t.y, t.vx, t.vy, t.radius, t.start, t.end,
                               max(t.uncertainty, 18+enemy.vel.length()*3), source="burst_turn_bound_uncalibrated")
                for t in super().forecast(enemy, snapshot)]


class ExplosiveBurst(BurstMover):
    # Type 250: local enums identify Ticking Spider; captured death + damage
    # flags verify the explosion. The radius remains a conservative estimate.
    death_effect_radius = 100


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


class EnemyAdapters:
    def __init__(self):
        self.default = GenericEnemy()
        self.adapters = {(25, 0): ExplosiveDeath(), (10, 0): Pursuer(), (18, 0): Pursuer(),
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
            self.threats.append(ThreatForecast("bomb:"+str(b.get("id", 0)), b["pos"]["x"], b["pos"]["y"],
                b.get("vel", {}).get("x", 0), b.get("vel", {}).get("y", 0), b["explosion_radius"],
                start=max(0, b["timer"]-3), end=max(0, b["timer"])+6, uncertainty=4))
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
                    0, 0, radius, end=6-(s.frame-death["frame"]), source="confirmed_death_approximate_radius"))
        self.buckets = defaultdict(set)
        for i, t in enumerate(self.threats):
            bins = range(8) if t.vx or t.vy else (-1,)
            for step in bins:
                begin, end = (max(0, step*6-1), min(45, (step+1)*6)) if step >= 0 else (0, 0)
                xs, ys = [t.x+t.vx*begin, t.x+t.vx*end], [t.y+t.vy*begin, t.y+t.vy*end]
                if t.segment_end: xs.append(t.segment_end[0]); ys.append(t.segment_end[1])
                r = t.radius+t.uncertainty
                for x in range(math.floor((min(xs)-r)/64), math.floor((max(xs)+r)/64)+1):
                    for y in range(math.floor((min(ys)-r)/64), math.floor((max(ys)+r)/64)+1): self.buckets[step, x, y].add(i)

    def query(self, a, b, radius, tick, begin=None):
        indices = set()
        for x in range(math.floor((min(a[0], b[0])-radius)/64), math.floor((max(a[0], b[0])+radius)/64)+1):
            for y in range(math.floor((min(a[1], b[1])-radius)/64), math.floor((max(a[1], b[1])+radius)/64)+1):
                indices.update(self.buckets.get((-1, x, y), ()))
                for step in range(min(7, int((tick if begin is None else begin)//6)), min(7, int(tick//6))+1):
                    indices.update(self.buckets.get((step, x, y), ()))
        return (self.threats[i] for i in indices)

    def gap(self, a, b, tick, radius):
        return min((t.gap(a, b, tick, radius) for t in self.query(a, b, radius, tick)), default=1000.)

    def collides(self, a, b, tick, radius, begin=None):
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

    def attack_safe(self, target, build, positions, hit):
        if target is None or hit <= 0: return True
        radius = self.adapters.resolve(target).death_radius(target)
        # Keep the blast reserve throughout an attack sequence. Waiting until
        # the final hit is too late when earlier tears are already in flight.
        if not radius: return True
        flight = max(1., target.pos.distance(self.snapshot.player.pos)/build.tear_speed)
        point = positions[min(len(positions)-1, round(flight))]
        predicted = target.pos+target.vel*min(flight, 12)
        return point.distance(predicted) > radius+self.snapshot.player.collision_radius+8
