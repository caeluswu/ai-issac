"""Persistent enemy-specific engagement; weapon components own attack timing."""
from dataclasses import dataclass, replace

from .models import Enemy, Vec


@dataclass(frozen=True)
class EngagementRoute:
    enemy: Enemy
    waypoints: tuple[Vec, ...]
    reason: str
    priority: int = 0
    phase: str = "approach"
    deadline: int | None = None


class ClosedHost:
    """Find a firing lane for a closed Host without pretending it can be hit."""

    def route(self, geometry, snapshot, enemy, build, position, weapon, excluded):
        proposal = position.route(geometry, snapshot, enemy.pos, build, weapon, excluded)
        if proposal.waypoints:
            return EngagementRoute(enemy, tuple(proposal.waypoints), "closed_host_firing_stance")
        # Exclusions are recovery hints, not a permanent ban. Once every
        # useful stance has been tried, revisit a real firing lane instead of
        # wandering around the room center while the Host stays closed.
        if excluded:
            proposal = position.route(geometry, snapshot, enemy.pos, build, weapon)
            if proposal.waypoints:
                return EngagementRoute(enemy, tuple(proposal.waypoints), "closed_host_retry_firing_stance")
        # Let the ordinary obstruction pipeline break reachable plain poop
        # instead of hiding the missing firing lane behind a reposition route.
        if enemy.vulnerable:
            return None
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
        if adapters is None:
            from .directional_armor import DirectionalArmor
            from .exposure import BuriedPin
            self.adapters = {(27,0):ClosedHost(),(27,1):ClosedHost(),
                             (62,0):BuriedPin(),
                             **{(41,v):DirectionalArmor() for v in (0,2,4)}}
        else:
            self.adapters = dict(adapters)
        self.preferred_id = None
        self.memory = {}
        self.scope = None

    def choose(self, geometry, snapshot, build, position, weapon, excluded=()):
        scope = (snapshot.run_id, snapshot.room_visit_id)
        if scope != self.scope:
            self.scope, self.memory, self.preferred_id = scope, {}, None
        # Other enemy families retain their own targeting policy. A Host opening
        # must not discard its route and restore generic kite scoring.
        other_attackable = any(enemy.vulnerable and (enemy.type, enemy.variant) not in self.adapters
                               for enemy in snapshot.hostiles)
        options = []
        for enemy in snapshot.hostiles:
            adapter = self.adapters.get((enemy.type, enemy.variant))
            if adapter is None:
                continue
            if other_attackable and getattr(adapter,'defer_to_unadapted',True):
                continue
            memory = self.memory.get(enemy.instance_id)
            if memory is None or memory["origin"].distance(enemy.pos) >= 16:
                memory = {"origin": enemy.pos, "blocked": {}, "goal": None,
                          "since": snapshot.frame, "progress": snapshot.frame,
                          "best": float("inf"), "hp": enemy.hp, "retry": False}
                self.memory[enemy.instance_id] = memory
            memory["blocked"] = {goal: until for goal, until in memory["blocked"].items()
                                 if until > snapshot.frame}
            if enemy.hp < memory["hp"]:
                memory.update(progress=snapshot.frame, since=snapshot.frame, retry=False)
                memory["blocked"].clear()
            memory["hp"] = enemy.hp
            if memory["goal"] is not None:
                wait_expired = snapshot.frame-memory["since"] >= 120
                stalled = snapshot.frame-memory["progress"] >= 60
                if wait_expired or stalled:
                    memory["blocked"][memory["goal"]] = snapshot.frame+180
                    memory.update(goal=None, best=float("inf"), since=snapshot.frame,
                                  progress=snapshot.frame)
            blocked = tuple(excluded)+tuple(memory["blocked"])
            route = adapter.route(geometry, snapshot, enemy, build, position, weapon, blocked)
            if route and route.reason == "closed_host_retry_firing_stance":
                retry_goal = (round(route.waypoints[-1].x), round(route.waypoints[-1].y))
                if memory["retry"] and memory["goal"] != retry_goal:
                    continue
                memory["retry"] = True
            if route:
                goal = (round(route.waypoints[-1].x), round(route.waypoints[-1].y))
                distance = sum(a.distance(b) for a,b in
                               zip((snapshot.player.pos,*route.waypoints),route.waypoints))
                if goal != memory["goal"]:
                    memory.update(goal=goal, since=snapshot.frame, progress=snapshot.frame,
                                  best=distance)
                if distance < memory["best"]-4:
                    memory.update(best=distance, progress=snapshot.frame)
                phase = "attack" if enemy.vulnerable else "wait_open" if distance < 14 else "approach"
                # Holding a valid firing stance is deliberate waiting, not a
                # failed movement. Its separate 120-update deadline still runs.
                if phase == "wait_open": memory["progress"] = snapshot.frame
                options.append(replace(route, phase=phase,
                    deadline=memory["since"]+120,
                    priority=route.priority+(0 if enemy.vulnerable else 2)))
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
