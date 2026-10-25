"""Grid geometry, swept segments, A* routing and reachable escape area."""
import heapq
import math
from collections import deque
from .models import Vec, WorldSnapshot


def segment_distance(point: Vec, start: Vec, end: Vec) -> float:
    dx, dy = end.x-start.x, end.y-start.y
    t = max(0., min(1., ((point.x-start.x)*dx+(point.y-start.y)*dy)/max(1e-9, dx*dx+dy*dy)))
    return math.hypot(point.x-start.x-t*dx, point.y-start.y-t*dy)


class GeometryService:
    def __init__(self, snapshot: WorldSnapshot, allowed_devices=()):
        self.cells = snapshot.layout["cells"]
        self.width = int(snapshot.layout["width"])
        self.fly = snapshot.stats["can_fly"]
        self.radius = snapshot.player.collision_radius+2
        self.physical_radius = snapshot.player.collision_radius
        self.origin = Vec(x=min(c["x"] for c in self.cells.values()), y=min(c["y"] for c in self.cells.values()))
        self.positions = {int(i): Vec(x=c["x"], y=c["y"]) for i, c in self.cells.items()}
        self.walkable, self.shootable = set(), set()
        self.dangerous = set()
        for i, c in self.cells.items():
            if not c["inside"]: continue
            i = int(i); collision = c["collision"]
            if collision in (0, 5) or (self.fly and collision in (1, 2)): self.walkable.add(i)
            if collision in (0, 1): self.shootable.add(i)
        # Spikes, trapdoors and pressure devices are not navigated in phase one.
        grid = snapshot.layout["grid"] or {}
        for i, c in grid.items():
            if c["type"] in (8, 9, 17, 20, 23, 25) and not (c["type"] == 20 and int(i) in allowed_devices):
                self.dangerous.add(int(i))
        self.walkable -= self.dangerous
        self.neighbors = {i: [j for j in (i-1, i+1, i-self.width, i+self.width)
                              if j in self.walkable and self.positions[i].distance(self.positions[j]) <= 41]
                          for i in self.walkable}

    def cell(self, p):
        return self.cell_xy(p.x, p.y)

    def cell_xy(self, x, y):
        return round((y-self.origin.y)/40)*self.width + round((x-self.origin.x)/40)

    def clear(self, p: Vec, *, shot=False, radius=None):
        allowed = self.shootable if shot else self.walkable
        r = (0 if shot else self.radius) if radius is None else radius
        return all(self.cell_xy(p.x+dx, p.y+dy) in allowed
                   for dx, dy in ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r),
                                  (r*.707, r*.707), (-r*.707, r*.707), (r*.707, -r*.707), (-r*.707, -r*.707)))

    def line_clear(self, a, b, *, shot=False, radius=None):
        n = max(1, math.ceil(a.distance(b)/8))
        allowed = self.shootable if shot else self.walkable
        r = 0 if shot else self.radius if radius is None else radius
        offsets = ((0, 0),) if shot else ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r),
                                        (r*.707, r*.707), (-r*.707, r*.707), (r*.707, -r*.707), (-r*.707, -r*.707))
        dx, dy = b.x-a.x, b.y-a.y
        return all(self.cell_xy(a.x+dx*i/n+ox, a.y+dy*i/n+oy) in allowed
                   for i in range(n+1) for ox, oy in offsets)

    def touches_danger(self, point):
        r = self.radius
        return any(self.cell_xy(point.x+dx, point.y+dy) in self.dangerous
                   for dx, dy in ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r),
                                  (r*.707, r*.707), (-r*.707, r*.707), (r*.707, -r*.707), (-r*.707, -r*.707)))

    def sweep(self, position, velocity):
        proposed = position+velocity
        if self.motion_clear(position, proposed): return proposed, velocity, False, False
        if self.touches_danger(proposed): return position, velocity, True, True
        # Solid terrain blocks movement; it does not inflict collision damage.
        # Resolve axes so the rollout can follow a wall instead of rejecting an
        # otherwise valid escape command at its first blocked update.
        x = Vec(x=proposed.x, y=position.y)
        if not self.motion_clear(position, x): x = position
        y = Vec(x=x.x, y=proposed.y)
        if not self.motion_clear(x, y): y = x
        adjusted = Vec(x=velocity.x if y.x != position.x else 0,
                       y=velocity.y if y.y != position.y else 0)
        return y, adjusted, True, False

    def motion_clear(self, start, end):
        if self.line_clear(start, end, radius=self.physical_radius): return True
        # Entity.Size is the NPC contact radius, not an exact grid footprint.
        # Knockback can leave a valid observed position inside our conservative
        # wall margin. Permit only motion out of existing overlap; never cross
        # a solid cell or enter a dangerous device to repair a model mismatch.
        if self.touches_danger(end) or self.cell(start) not in self.walkable: return False
        r = self.physical_radius
        offsets = ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r),
                   (r*.707, r*.707), (-r*.707, r*.707), (r*.707, -r*.707), (-r*.707, -r*.707))
        bad = {n for n, (x, y) in enumerate(offsets) if self.cell_xy(start.x+x, start.y+y) not in self.walkable}
        if not bad: return False
        # Compare the distance to a safe cell center for every offending sample.
        # Tangential sliding is allowed; movement deeper into a wall is not.
        for n, (x, y) in enumerate(offsets):
            a, b = Vec(x=start.x+x, y=start.y+y), Vec(x=end.x+x, y=end.y+y)
            if n not in bad:
                if not self.line_clear(a, b, radius=0): return False
            elif self.cell(b) not in self.walkable:
                nearest = self.positions[self.nearest(a)]
                if b.distance(nearest) > a.distance(nearest)+1e-6: return False
        return self.cell(end) in self.walkable

    def nearest(self, p):
        if not self.walkable: raise ValueError("No walkable cells")
        return min(self.walkable, key=lambda i: self.positions[i].distance(p))

    def path(self, start, goal):
        a, b = self.nearest(start), self.nearest(goal)
        queue, cost, parent = [(0, a)], {a: 0}, {}
        while queue:
            _, i = heapq.heappop(queue)
            if i == b:
                result = [i]
                while i in parent: i = parent[i]; result.append(i)
                return [self.positions[x] for x in reversed(result)]
            for j in self.neighbors[i]:
                if cost[i]+40 < cost.get(j, math.inf):
                    cost[j], parent[j] = cost[i]+40, i
                    heapq.heappush(queue, (cost[j]+self.positions[j].distance(goal), j))
        return []

    def future_safe_cells(self, enemies, horizon):
        safe = set(self.walkable)
        for i in self.walkable:
            p = self.positions[i]
            for enemy in enemies:
                if enemy.collision_class == 0: continue
                future = enemy.pos + enemy.vel*min(horizon, 12)
                if segment_distance(p, enemy.pos, future) < enemy.collision_radius+self.radius+8:
                    safe.discard(i); break
        return safe

    def firing_route(self, start, target, effective_range, terrain=False, excluded=()):
        """Reach a cardinal firing lane, including around obstructing rocks."""
        origin = self.nearest(start)
        queue, parents, depth = deque([origin]), {}, {origin: 0}
        best = None
        while queue:
            i = queue.popleft(); p = self.positions[i]
            # Project into exact cardinal alignment inside each walkable cell.
            # A grid center within 14px is not necessarily a hittable stance.
            for candidate in (Vec(x=p.x, y=target.y), Vec(x=target.x, y=p.y)):
                if candidate.distance(p) > 19 or (round(candidate.x), round(candidate.y)) in excluded: continue
                delta = target-candidate; distance = delta.length()
                if (25 if terrain else 60) <= distance <= effective_range*.85:
                    end = target-delta.unit()*22 if terrain else target
                    if self.clear(candidate) and self.line_clear(p, candidate) and self.line_clear(candidate, end, shot=True):
                        score = depth[i]*40+p.distance(candidate)+abs(distance-effective_range*.55)*.15
                        if best is None or score < best[0]: best = (score, i, candidate)
            for j in self.neighbors.get(i, []):
                if j not in depth:
                    depth[j], parents[j] = depth[i]+1, i; queue.append(j)
        if best is None: return []
        path, i = [best[2]], best[1]
        while True:
            path.append(self.positions[i])
            if i not in parents: break
            i = parents[i]
        return list(reversed(path))

    def reachable_fraction(self, position, safe, max_steps):
        origin = self.cell(position)
        if origin not in safe: return 0.
        found, q = {origin}, deque([(origin, 0)])
        while q:
            i, depth = q.popleft()
            if depth >= max_steps: continue
            for j in self.neighbors.get(i, []):
                if j in safe and j not in found:
                    found.add(j); q.append((j, depth+1))
        # Compare against geometrically reachable area in the same time budget.
        total, q = {origin}, deque([(origin, 0)])
        while q:
            i, depth = q.popleft()
            if depth >= max_steps: continue
            for j in self.neighbors.get(i, []):
                if j not in total: total.add(j); q.append((j, depth+1))
        return len(found)/max(1, len(total))


def door_route(s, visited):
    g = GeometryService(s)
    routes = []
    for slot, door in (s.layout["doors"] or {}).items():
        if not door["is_open"] or door["is_locked"] or door["target_room_type"] != 1 or door["target_room"] in visited:
            continue
        point = Vec(x=door["x"], y=door["y"])
        path = g.path(s.player.pos, point)
        if path:
            # Door centers may lie in a boundary cell. Aim just inside first,
            # then continue through the open door with normal movement.
            routes.append((sum(a.distance(b) for a, b in zip([s.player.pos]+path, path)), int(slot), door, path))
    return min(routes, key=lambda x: (x[0], x[1])) if routes else None
