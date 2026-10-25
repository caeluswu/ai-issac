"""Time-bounded threats, injectable enemy adapters and spatial broad phase."""
from collections import defaultdict, deque
from dataclasses import dataclass
import math
from .models import Vec


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

    def gap(self, a, b, tick, player_radius):
        if tick < self.start or tick > self.end: return 1000.
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
        ax, ay = a[0]-self.x-self.vx*max(0, tick-1), a[1]-self.y-self.vy*max(0, tick-1)
        bx, by = b[0]-self.x-self.vx*tick, b[1]-self.y-self.vy*tick
        return distance_segment(0, 0, ax, ay, bx, by)-self.radius-player_radius-self.uncertainty


class GenericEnemy:
    """Unknown phases remain unknown; current velocity has bounded error."""
    def forecast(self, enemy, snapshot):
        if enemy.collision_class == 0: return []
        return [ThreatForecast(enemy.instance_id, enemy.pos.x, enemy.pos.y, enemy.vel.x, enemy.vel.y,
                               enemy.collision_radius, uncertainty=4+min(8, enemy.vel.length()*2),
                               source="observed_motion_approximation")]
    def death_radius(self, enemy): return 0


class ExplosiveDeath(GenericEnemy):
    """Normal Boom Fly only; 100px is a conservative uncalibrated model bound."""
    def death_radius(self, enemy): return 100


class EnemyAdapters:
    def __init__(self):
        self.default = GenericEnemy()
        self.adapters = {(25, 0): ExplosiveDeath()}
    def resolve(self, enemy): return self.adapters.get((enemy.type, enemy.variant), self.default)
    def register(self, kind, variant, adapter): self.adapters[kind, variant] = adapter


class ThreatField:
    def __init__(self, s, adapters=None):
        self.snapshot = s
        self.adapters = adapters or EnemyAdapters()
        self.threats = []
        for enemy in s.hostiles: self.threats.extend(self.adapters.resolve(enemy).forecast(enemy, s))
        for i, p in enumerate(s.projectiles["enemy_projectiles"] or []):
            self.threats.append(ThreatForecast("bullet:"+str(p.get("instance_id", i)),
                p["pos"]["x"], p["pos"]["y"], p["vel"]["x"], p["vel"]["y"], p["collision_radius"], uncertainty=3))
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
            if death.get("type") == 25 and death.get("variant") == 0 and s.frame-death["frame"] <= 6:
                self.threats.append(ThreatForecast("death:"+death["instance_id"], death["pos"]["x"], death["pos"]["y"],
                    0, 0, 100, end=6-(s.frame-death["frame"]), source="confirmed_death_approximate_radius"))
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

    def query(self, a, b, radius, tick):
        indices = set()
        for x in range(math.floor((min(a[0], b[0])-radius)/64), math.floor((max(a[0], b[0])+radius)/64)+1):
            for y in range(math.floor((min(a[1], b[1])-radius)/64), math.floor((max(a[1], b[1])+radius)/64)+1):
                indices.update(self.buckets.get((-1, x, y), ()))
                indices.update(self.buckets.get((min(7, int(tick//6)), x, y), ()))
        return (self.threats[i] for i in indices)

    def gap(self, a, b, tick, radius):
        return min((t.gap(a, b, tick, radius) for t in self.query(a, b, radius, tick)), default=1000.)

    def collides(self, a, b, tick, radius):
        return any(t.gap(a, b, tick, radius) <= 0 for t in self.query(a, b, radius, tick))

    def escape(self, geometry, position, speed, radius, start_tick=12):
        origin = geometry.cell(position)
        if origin not in geometry.walkable: return 0., -45., 0
        # Time-expanded reachability: each traversed edge is checked at its
        # actual arrival time, including the six-update withdrawal reserve.
        queue, seen, directions = deque([(origin, start_tick, None)]), {origin}, set()
        first = geometry.positions[origin]
        while queue and len(seen) <= 48:
            cell, tick, direction = queue.popleft()
            for neighbor in geometry.neighbors.get(cell, []):
                if neighbor in seen: continue
                arrival = tick+40/max(1., speed)
                if arrival+6 > 45: continue
                a, b = geometry.positions[cell], geometry.positions[neighbor]
                valid = all(not self.collides((a.x, a.y), (b.x, b.y), t, radius)
                            for t in (tick, arrival, arrival+6))
                if not valid: continue
                seen.add(neighbor)
                branch = direction if direction is not None else neighbor
                if b.distance(first) >= 60: directions.add(branch)
                queue.append((neighbor, arrival, branch))
        count = len(directions)
        fraction = min(1., count/2)
        # Escape slack is the hold margin at the candidate endpoint; a viable
        # path is also required, so static free-area ratio cannot mask enclosure.
        point = (position.x, position.y)
        ttc = next((t for t in range(start_tick, 46, 3) if self.collides(point, point, t, radius)), 51)
        slack = ttc-start_tick-60/max(1., speed) if count else -45.
        return fraction, slack, count

    def attack_safe(self, target, build, positions, hit):
        if target is None or hit <= 0: return True
        radius = self.adapters.resolve(target).death_radius(target)
        if not radius or target.hp > float(self.snapshot.stats.get("damage", 3.5)): return True
        flight = max(1., target.pos.distance(self.snapshot.player.pos)/build.tear_speed)
        point = positions[min(len(positions)-1, round(flight))]
        predicted = target.pos+target.vel*min(flight, 12)
        return point.distance(predicted) > radius+self.snapshot.player.collision_radius+8
