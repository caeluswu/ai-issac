"""Bounded repositioning to elicit an observable attackable phase."""


class MomWallStance:
    """Prepare a firing lane before a wall part opens; preserve hand clearance."""
    def route(self, geometry, snapshot, enemy, build, position, weapon, excluded):
        from .engagement import EngagementRoute
        if enemy.state not in (3,8,9,10): return None
        minimum = min(140,build.effective_range*.7)
        def lane(hints):
            return geometry.firing_route(snapshot.player.pos,enemy.pos,build.effective_range,
                excluded=hints,target_radius=enemy.collision_radius+5,
                stance_filter=lambda p:p.distance(enemy.pos)>=minimum)
        route = lane(excluded) or (lane(()) if excluded else [])
        if route:
            return EngagementRoute(enemy,tuple(route),'mom_wall_firing_stance')
        return None


class BuriedPin:
    def route(self, geometry, snapshot, enemy, build, position, weapon, excluded):
        from .engagement import EngagementRoute
        # State 3 parts are followers; state 4/StateFrame 1 is the buried head.
        if enemy.vulnerable or enemy.state != 4 or enemy.collision_class != 0:
            return None
        distance = min(150,build.effective_range*.7)
        route = geometry.firing_route(snapshot.player.pos,enemy.pos,build.effective_range,
            excluded=excluded,stance_filter=lambda p:p.distance(enemy.pos)>=distance)
        if not route and excluded:
            route = geometry.firing_route(snapshot.player.pos,enemy.pos,build.effective_range,
                stance_filter=lambda p:p.distance(enemy.pos)>=distance)
        if route:
            return EngagementRoute(enemy,tuple(route),'buried_pin_exposure_probe')
        return None
