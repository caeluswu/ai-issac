"""Bounded repositioning to elicit an observable attackable phase."""


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
