"""Grid geometry, swept segments, A* routing and reachable escape area."""
import heapq
import math
from collections import deque, OrderedDict
from .models import Vec, WorldSnapshot


def segment_distance(point: Vec, start: Vec, end: Vec) -> float:
    dx, dy = end.x-start.x, end.y-start.y
    t = max(0., min(1., ((point.x-start.x)*dx+(point.y-start.y)*dy)/max(1e-9, dx*dx+dy*dy)))
    return math.hypot(point.x-start.x-t*dx, point.y-start.y-t*dy)


class GeometryService:
    def __init__(self, snapshot: WorldSnapshot, allowed_devices=(), allowed_exits=()):
        self.cells = snapshot.layout["cells"]
        self.width = int(snapshot.layout["width"])
        self.fly = snapshot.stats["can_fly"]
        self.radius = snapshot.player.collision_radius+2
        self.physical_radius = snapshot.player.collision_radius
        self.pickup_obstacles = tuple((Vec(x=p["x"],y=p["y"]),p["radius"]) for p in getattr(snapshot,"pickup_exclusions",[]))
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
            authorized = (c["type"] == 20 and int(i) in allowed_devices) or (
                c["type"] == 17 and c.get("variant") == 0 and int(i) in allowed_exits)
            if c["type"] in (8, 9, 17, 20, 23, 25) and not authorized:
                self.dangerous.add(int(i))
        self.dangerous.update(self.cell(Vec(**h["pos"])) for h in snapshot.hazards
                              if h.get("type") == "FIREPLACE" and not h.get("is_extinguished", False))
        self.walkable -= self.dangerous
        self.motion_cells = set(self.walkable)
        self.walkable = {i for i in self.walkable if all(self.positions[i].distance(p)>r+self.radius for p,r in self.pickup_obstacles)}
        self.neighbors = {i: [j for j in (i-1, i+1, i-self.width, i+self.width)
                              if j in self.walkable and self.positions[i].distance(self.positions[j]) <= 41]
                          for i in self.walkable}
        self.nearest_candidates = {}
        # GeometryService is replaced when terrain/fire/flight permissions
        # change. Cache exact queries only, with bounded per-room memory.
        self.line_cache = {}
        self.firing_stance_cache = OrderedDict()
        self.portals = tuple((int(slot), Vec(x=d['x'], y=d['y'])) for slot, d in (snapshot.layout['doors'] or {}).items()
                             if d['is_open'] and not d['is_locked'])

    def cell(self, p):
        return self.cell_xy(p.x, p.y)

    def cell_xy(self, x, y):
        return round((y-self.origin.y)/40)*self.width + round((x-self.origin.x)/40)

    def clear(self, p: Vec, *, shot=False, radius=None):
        allowed = self.shootable if shot else self.motion_cells
        r = (0 if shot else self.radius) if radius is None else radius
        if not shot and any(p.distance(center)<=r+size for center,size in self.pickup_obstacles): return False
        return all(self.cell_xy(p.x+dx, p.y+dy) in allowed
                   for dx, dy in ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r),
                                  (r*.707, r*.707), (-r*.707, r*.707), (r*.707, -r*.707), (-r*.707, -r*.707)))

    def line_clear(self, a, b, *, shot=False, radius=None):
        r = 0 if shot else self.radius if radius is None else radius
        key = (a.x, a.y, b.x, b.y, shot, r)
        cached = self.line_cache.get(key)
        if cached is not None: return cached
        if not shot and not self.pickup_path_clear(a,b,r): return False
        n = max(1, math.ceil(math.hypot(b.x-a.x, b.y-a.y)/8))
        allowed = self.shootable if shot else self.motion_cells
        offsets = ((0, 0),) if shot else ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r),
                                        (r*.707, r*.707), (-r*.707, r*.707), (r*.707, -r*.707), (-r*.707, -r*.707))
        dx, dy = b.x-a.x, b.y-a.y
        result = all(self.cell_xy(a.x+dx*i/n+ox, a.y+dy*i/n+oy) in allowed
                     for i in range(n+1) for ox, oy in offsets)
        if len(self.line_cache) >= 4096: self.line_cache.clear()
        self.line_cache[key] = result
        return result

    def pickup_path_clear(self, start, end, radius=None):
        radius = self.physical_radius if radius is None else radius
        for point, size in self.pickup_obstacles:
            margin = radius+size+2
            if segment_distance(point,start,end) > margin: continue
            # A newly spawned item may already overlap the player. Only allow
            # monotonic withdrawal, never use wall/portal repair to enter it.
            if start.distance(point) < margin and (end-start).dot(start-point) >= 0 and end.distance(point)>start.distance(point): continue
            return False
        return True

    def touches_danger(self, point):
        if not self.dangerous: return False
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
        if not self.pickup_path_clear(start,end): return False
        if self.line_clear(start, end, radius=self.physical_radius): return True
        if self.retreat_from_danger(start, end): return True
        if self.retreat_from_portal(start, end): return True
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
        steps = max(1, math.ceil(math.hypot(end.x-start.x, end.y-start.y)/8))
        for n, (x, y) in enumerate(offsets):
            ax, ay, bx, by = start.x+x, start.y+y, end.x+x, end.y+y
            if n not in bad:
                if any(self.cell_xy(ax+(bx-ax)*step/steps, ay+(by-ay)*step/steps) not in self.walkable
                       for step in range(1, steps+1)): return False
            elif self.cell_xy(bx, by) not in self.walkable:
                nearest = self.positions[self.nearest(Vec(x=ax, y=ay))]
                if (bx-nearest.x)**2+(by-nearest.y)**2 > (ax-nearest.x)**2+(ay-nearest.y)**2+1e-6: return False
        return self.cell(end) in self.walkable

    def retreat_from_danger(self, start, end):
        """Permit monotonic withdrawal from a newly occupied forbidden grid cell.

        Boss exits and fire can appear under the observed player. The existing
        overlap is not permission to move further into that cell or through a
        different forbidden cell.
        """
        r = self.physical_radius
        offsets = ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r),
                   (r*.707, r*.707), (-r*.707, r*.707), (r*.707, -r*.707), (-r*.707, -r*.707))
        overlap = {cell for dx, dy in offsets
                   if (cell := self.cell_xy(start.x+dx, start.y+dy)) in self.dangerous
                   and cell in self.positions}
        if not overlap: return False
        steps = max(1, math.ceil(start.distance(end)/8))
        allowed = self.motion_cells | overlap
        previous = {cell: start.distance(self.positions[cell]) for cell in overlap}
        for step in range(steps+1):
            point = Vec(x=start.x+(end.x-start.x)*step/steps,
                        y=start.y+(end.y-start.y)*step/steps)
            if any(self.cell_xy(point.x+dx, point.y+dy) not in allowed for dx, dy in offsets):
                return False
            if step and any(point.distance(self.positions[cell]) <= previous[cell]+1e-3 for cell in overlap):
                return False
            previous = {cell: point.distance(self.positions[cell]) for cell in overlap}
        return True

    def retreat_from_portal(self, start, end):
        """Allow an observed body in an open aperture to move into this room.

        Portal centers sit outside the interior walkable grid. Knockback and
        drift can leave an otherwise legal player there. This narrow exception
        never authorizes outward crossing; DoorNavigator owns that operation.
        """
        r = self.physical_radius
        offsets = ((0,0),(r,0),(-r,0),(0,r),(0,-r),
                   (r*.707,r*.707),(-r*.707,r*.707),(r*.707,-r*.707),(-r*.707,-r*.707))
        dx, dy = end.x-start.x, end.y-start.y
        steps = max(1, math.ceil(math.hypot(dx,dy)/8))
        for slot, point in self.portals:
            ix, iy = ((1,0),(0,1),(-1,0),(0,-1))[slot%4]
            if start.distance(point) > 45 or dx*ix+dy*iy <= 1e-6: continue
            valid = True
            for step in range(steps+1):
                for ox, oy in offsets:
                    x, y = start.x+dx*step/steps+ox, start.y+dy*step/steps+oy
                    cell = self.cell_xy(x,y)
                    if cell in self.dangerous: valid = False; break
                    if cell in self.walkable: continue
                    inward = (x-point.x)*ix+(y-point.y)*iy
                    lateral = abs((x-point.x)*iy-(y-point.y)*ix)
                    if not (-2*r <= inward <= 60 and lateral <= 20): valid = False; break
                if not valid: break
            if valid: return True
        return False

    def nearest(self, p):
        if not self.walkable: raise ValueError("No walkable cells")
        cell = self.cell(p)
        if cell in self.walkable: return cell
        candidates = self.nearest_candidates.get(cell)
        if candidates is None:
            center = self.positions.get(cell)
            if center is None:
                return min(self.walkable, key=lambda i: (self.positions[i].x-p.x)**2+(self.positions[i].y-p.y)**2)
            distances = {i: math.hypot(self.positions[i].x-center.x, self.positions[i].y-center.y) for i in self.walkable}
            # Within a 40px cell, |p-center| <= 20*sqrt(2). By the
            # triangle inequality, farther centers cannot become nearest.
            bound = min(distances.values())+40*math.sqrt(2)+1e-6
            candidates = tuple(i for i, distance in distances.items() if distance <= bound)
            self.nearest_candidates[cell] = candidates
        return min(candidates, key=lambda i: (self.positions[i].x-p.x)**2+(self.positions[i].y-p.y)**2)

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

    def firing_stances(self, target, effective_range, terrain):
        """Geometry-only candidates; origin/recovery exclusions stay dynamic.

        Many ordinary-poop targets share one unchanged room geometry. Compute
        their legal cardinal stances once instead of constructing vectors for
        every rejected projection on every frame. The bounded LRU keeps static
        targets hot while moving enemies cannot grow the cache indefinitely.
        """
        key = (target.x, target.y, effective_range, terrain)
        if key in self.firing_stance_cache:
            self.firing_stance_cache.move_to_end(key)
            return self.firing_stance_cache[key]
        stances = {}
        for i in self.walkable:
            p = self.positions[i]
            for x, y in ((p.x, target.y), (target.x, p.y)):
                offset = math.hypot(x-p.x, y-p.y)
                if offset > 19: continue
                dx, dy = target.x-x, target.y-y
                distance = math.hypot(dx, dy)
                if not (25 if terrain else 60) <= distance <= effective_range*.85: continue
                candidate = Vec(x=x, y=y)
                end = Vec(x=target.x-dx/distance*22, y=target.y-dy/distance*22) if terrain else target
                if self.clear(candidate) and self.line_clear(p, candidate) and self.line_clear(candidate, end, shot=True):
                    stances.setdefault(i, []).append((candidate, offset, abs(distance-effective_range*.55)*.15))
        if len(self.firing_stance_cache) >= 64: self.firing_stance_cache.popitem(last=False)
        self.firing_stance_cache[key] = stances
        return stances

    def firing_route(self, start, target, effective_range, terrain=False, excluded=()):
        """Reach a cardinal firing lane, including around obstructing rocks."""
        stances = self.firing_stances(target, effective_range, terrain)
        if not stances: return []
        origin = self.nearest(start)
        queue, parents, depth = deque([origin]), {}, {origin: 0}
        best = None
        while queue:
            i = queue.popleft(); p = self.positions[i]
            # BFS depth is nondecreasing; the remaining score terms are
            # nonnegative. No deeper stance can improve the current minimum.
            # Keep equal-depth candidates so deterministic tie order survives.
            if best is not None and depth[i]*40 > best[0]: break
            for candidate, offset, bias in stances.get(i, ()):
                if (round(candidate.x), round(candidate.y)) in excluded: continue
                score = depth[i]*40+offset+bias
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

    def reposition_route(self, start, target, excluded=()):
        """Change lane when every current shot is occluded; never shoot rocks.

        A following enemy can then leave its occluded band. The goal remains
        fixed until reached/rejected, so motion earns normal bounded progress.
        """
        center_x = sum(self.positions[i].x for i in self.walkable)/len(self.walkable)
        center_y = sum(self.positions[i].y for i in self.walkable)/len(self.walkable)
        choices = sorted(self.walkable, key=lambda i: (self.positions[i].x-center_x)**2+(self.positions[i].y-center_y)**2)
        for i in choices:
            point = self.positions[i]
            if point.distance(start) < 80 or point.distance(target) < 60 or (round(point.x), round(point.y)) in excluded: continue
            route = self.path(start, point)
            if route: return route
        return []

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


def door_route(s, visited, *, target_room=None):
    g = GeometryService(s)
    routes = []
    for slot, door in (s.layout["doors"] or {}).items():
        if not door["is_open"] or door["is_locked"] or door["target_room_type"] != 1:
            continue
        if target_room is None and door["target_room"] in visited: continue
        if target_room is not None and door["target_room"] != target_room: continue
        point = Vec(x=door["x"], y=door["y"])
        path = g.path(s.player.pos, point)
        if path:
            # Door centers may lie in a boundary cell. Aim just inside first,
            # then continue through the open door with normal movement.
            routes.append((sum(a.distance(b) for a, b in zip([s.player.pos]+path, path)), int(slot), door, path))
    return min(routes, key=lambda x: (x[0], x[1])) if routes else None
