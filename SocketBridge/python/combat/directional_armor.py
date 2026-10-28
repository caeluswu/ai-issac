"""Observed incoming-velocity permissions and separate flanking geometry."""
from collections import deque
import math

SAMPLES = tuple((x,y,f'{x},{y}',math.hypot(x,y))
    for y in (-1,0,1) for x in (-1,0,1) if x or y)


def damage_from_velocity(enemy, velocity):
    """Nearest observed octant, not a prediction that armor cannot rotate."""
    flags = enemy.damage_directions
    if not flags or velocity.length() < 1e-6: return None
    sample = max(SAMPLES,key=lambda d:(velocity.x*d[0]+velocity.y*d[1])/d[3])
    return flags.get(sample[2])


class DirectionalArmor:
    """Find a permitted firing stance without routing through the target."""
    defer_to_unadapted = False
    def route(self, geometry, snapshot, enemy, build, position, weapon, excluded):
        from .engagement import EngagementRoute
        flags = enemy.damage_directions
        if build.family == 'knife' or not flags or not any(v is False for v in flags.values()):
            return None
        stances = geometry.firing_stances(enemy.pos,build.effective_range,False)
        allowed = geometry.future_safe_cells(snapshot.hostiles,6)
        start = geometry.nearest(snapshot.player.pos)
        queue, parents, depths, best = deque([start]), {}, {start:0}, None
        while queue:
            index = queue.popleft()
            for point,offset,range_cost in stances.get(index,()):
                if (round(point.x),round(point.y)) in excluded: continue
                if damage_from_velocity(enemy,enemy.pos-point) is not True: continue
                cost = depths[index]*40+offset+range_cost
                if best is None or cost < best[0]: best = cost,index,point
            for neighbor in geometry.neighbors.get(index,()):
                if neighbor in allowed and neighbor not in depths:
                    depths[neighbor] = depths[index]+1
                    parents[neighbor] = index
                    queue.append(neighbor)
        if best is None:
            return self.route(geometry,snapshot,enemy,build,position,weapon,()) if excluded else None
        _, index, point = best
        path = [point]
        while True:
            path.append(geometry.positions[index])
            if index not in parents: break
            index = parents[index]
        return EngagementRoute(enemy,tuple(reversed(path)),'directional_armor_flank')
