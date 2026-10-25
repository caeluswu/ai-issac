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
    def __init__(self, snapshot: WorldSnapshot):
        self.cells = snapshot.layout["cells"]
        self.width = int(snapshot.layout["width"])
        self.fly = snapshot.stats["can_fly"]
        self.radius = snapshot.player.collision_radius+2
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
            if c["type"] in (8, 9, 17, 20, 23): self.dangerous.add(int(i))
        self.walkable -= self.dangerous
        self.neighbors = {i: [j for j in (i-1, i+1, i-self.width, i+self.width)
                              if j in self.walkable and self.positions[i].distance(self.positions[j]) <= 41]
                          for i in self.walkable}

    def cell(self, p):
        return round((p.y-self.origin.y)/40)*self.width + round((p.x-self.origin.x)/40)

    def clear(self, p: Vec, *, shot=False, radius=None):
        allowed = self.shootable if shot else self.walkable
        r = (0 if shot else self.radius) if radius is None else radius
        return all(self.cell(Vec(x=p.x+dx, y=p.y+dy)) in allowed
                   for dx, dy in ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r),
                                  (r*.707, r*.707), (-r*.707, r*.707), (r*.707, -r*.707), (-r*.707, -r*.707)))

    def line_clear(self, a, b, *, shot=False):
        n = max(1, math.ceil(a.distance(b)/8))
        return all(self.clear(a+(b-a)*(i/n), shot=shot) for i in range(n+1))

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
