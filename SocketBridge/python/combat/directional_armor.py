"""Observed incoming-velocity permissions and separate flanking geometry."""
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
        allowed = geometry.future_safe_cells(snapshot.hostiles,6)
        path = geometry.firing_route(snapshot.player.pos,enemy.pos,build.effective_range,
            excluded=excluded,allowed_cells=allowed,
            stance_filter=lambda p:damage_from_velocity(enemy,enemy.pos-p) is True)
        if not path:
            return self.route(geometry,snapshot,enemy,build,position,weapon,()) if excluded else None
        return EngagementRoute(enemy,tuple(path),'directional_armor_flank')
