"""Enemy-specific movement goals while no enemy is currently attackable."""
from dataclasses import dataclass

from .models import Enemy, Vec


@dataclass(frozen=True)
class EngagementRoute:
    enemy: Enemy
    waypoints: tuple[Vec, ...]
    reason: str
    priority: int = 0


class ClosedHost:
    """Find a firing lane for a closed Host without pretending it can be hit."""

    def route(self, geometry, snapshot, enemy, build, position, weapon, excluded):
        proposal = position.route(geometry, snapshot, enemy.pos, build, weapon, excluded)
        if proposal.waypoints:
            return EngagementRoute(enemy, tuple(proposal.waypoints), "closed_host_firing_stance")
        reposition = getattr(position, "reposition", None)
        if reposition is None:
            return None
        proposal = reposition(geometry, snapshot, enemy.pos, build, excluded)
        if proposal.waypoints:
            return EngagementRoute(enemy, tuple(proposal.waypoints), "closed_host_reposition", priority=1)
        return None


class EngagementPlanner:
    """Replaceable per-enemy engagement adapters; attack permission stays separate."""

    def __init__(self, adapters=None):
        self.adapters = {(27, 0): ClosedHost()} if adapters is None else dict(adapters)
        self.preferred_id = None

    def choose(self, geometry, snapshot, build, position, weapon, excluded=()):
        if any(enemy.vulnerable for enemy in snapshot.hostiles):
            self.preferred_id = None
            return None
        options = []
        for enemy in snapshot.hostiles:
            adapter = self.adapters.get((enemy.type, enemy.variant))
            if adapter is None:
                continue
            route = adapter.route(geometry, snapshot, enemy, build, position, weapon, excluded)
            if route:
                options.append(route)
        if not options:
            self.preferred_id = None
            return None
        best_priority = min(route.priority for route in options)
        options = [route for route in options if route.priority == best_priority]
        preferred = next((route for route in options if route.enemy.instance_id == self.preferred_id), None)
        selected = preferred or min(options, key=lambda route: (
            sum(a.distance(b) for a, b in zip((snapshot.player.pos, *route.waypoints), route.waypoints)),
            route.enemy.instance_id,
        ))
        self.preferred_id = selected.enemy.instance_id
        return selected
