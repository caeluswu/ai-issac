"""Replaceable pure policy and weapon modules; no IO or controller references."""
from dataclasses import dataclass
from typing import Protocol
from .models import Vec, ZERO


@dataclass(frozen=True)
class CapabilitySet:
    family: str
    moving_attack: bool = True
    break_plain_poop: bool = True


@dataclass(frozen=True)
class AttackProposal:
    shoot: tuple[int, int]
    hit_score: float
    expected_damage: float
    reason: str
    wait: "WaitEvidence | None" = None


@dataclass(frozen=True)
class WaitEvidence:
    token: str
    reason: str
    deadline_frame: int
    evidence: str


class WeaponAdapter(Protocol):
    capabilities: CapabilitySet
    def propose(self, s, build, profile, target, velocity, geometry) -> list[AttackProposal]: ...


class Registry:
    def __init__(self): self.factories = {}
    def register(self, name, factory):
        if name in self.factories: raise ValueError("Duplicate executable component: "+name)
        self.factories[name] = factory
    def create(self, choice):
        if choice.component not in self.factories: raise ValueError("Missing executable component: "+choice.component)
        return self.factories[choice.component](dict(choice.params))


class Target:
    def __init__(self, params): self.params = params
    def features(self, enemy, s, build):
        distance = enemy.pos.distance(s.player.pos)
        threat = min(1., (enemy.vel.length()+1)*30/max(30, distance))
        finish = min(1., float(s.stats.get("damage", 3.5))/max(.1, enemy.hp))
        approach = 1-min(1., distance/max(1, build.effective_range))
        return threat, finish, approach


class ThreatFirst(Target):
    def score(self, enemy, s, build):
        a, b, c = self.features(enemy, s, build); return .7*a+.2*b+.1*c


class FinishFirst(Target):
    def score(self, enemy, s, build):
        a, b, c = self.features(enemy, s, build); return .1*a+.7*b+.2*c


class WeightedTarget(Target):
    def score(self, enemy, s, build):
        return sum(a*self.params[b] for a, b in zip(self.features(enemy, s, build),
                   ("threat_weight", "finish_weight", "approach_cost_weight")))


class KeepRange:
    def __init__(self, params): self.params = params
    def score(self, delta, desired, s, build):
        low, high = (self.params[x]*build.effective_range for x in ("min_range_ratio", "max_range_ratio"))
        return -max(low-delta.length(), 0, delta.length()-high)/50-min(abs(delta.x), abs(delta.y))/90


class Kite(KeepRange):
    def score(self, delta, desired, s, build):
        return super().score(delta, desired, s, build)+min(delta.length(), self.params["min_range_ratio"]*build.effective_range)/150


class Strafe(KeepRange):
    def score(self, delta, desired, s, build):
        side = self.params["preferred_side"]
        sign = -1 if side == "left" else 1
        if side == "auto": sign = 1 if (s.frame//self.params["side_switch_ticks"]) % 2 else -1
        return super().score(delta, desired, s, build)+sign*(desired.x*delta.y-desired.y*delta.x)/max(1, delta.length()*build.move_speed)*.5


class DirectAim:
    def __init__(self, params): self.params = params
    def velocity(self, target): return ZERO


class PredictiveAim(DirectAim):
    def velocity(self, target): return target.vel


class MaxClearance:
    def __init__(self, params): self.params = params
    def score(self, clearance, desired): return min(24, max(0, clearance))/150


class MinimalShift(MaxClearance):
    def score(self, clearance, desired): return -desired.length()*.015


class KeepRoutes:
    def __init__(self, params): self.params = params
    def score(self, fraction): return min(.7, fraction)*.8-(2 if fraction < self.params["min_safe_area_ratio"] else 0)


class LocalRepath:
    def __init__(self, params): self.params = params
    def route_index(self, level, failures, count): return min(max(0, count-1), failures) if level else 0


class NormalTears:
    capabilities = CapabilitySet("normal_tears")
    directions = ((1, 0), (-1, 0), (0, 1), (0, -1))
    def __init__(self, params): self.params = params
    def hit_score(self, s, build, aim, target, shot, velocity, geometry):
        if target is None or not target.vulnerable: return 0.
        launch = Vec(x=shot[0], y=shot[1])*build.tear_speed
        if aim.params["movement_inheritance"]:
            measured = s.movement_inheritance.get(f"{shot[0]},{shot[1]}")
            launch += (Vec(**measured)+(velocity-s.player.vel)*.5) if measured else velocity*.5
        enemy_velocity = aim.velocity(target)
        relative = launch-enemy_velocity
        delta = target.pos-s.player.pos
        ticks = delta.dot(relative)/max(1e-9, relative.dot(relative))
        forecast_ticks = min(max(0, ticks), aim.params["max_lead_ticks"])
        if ticks > forecast_ticks and enemy_velocity.length() > 0:
            delta = target.pos+enemy_velocity*forecast_ticks-s.player.pos
            ticks = delta.dot(launch)/max(1e-9, launch.dot(launch))
        if ticks < 0 or ticks*launch.length() > build.effective_range: return 0.
        hit = s.player.pos+launch*ticks
        miss = hit.distance(target.pos+enemy_velocity*min(ticks, aim.params["max_lead_ticks"]))
        miss += enemy_velocity.length()*max(0, ticks-forecast_ticks)*.2
        margin = target.collision_radius+5
        end = hit-(hit-s.player.pos).unit()*22 if target.instance_id.startswith("terrain:") else hit
        if not geometry.line_clear(s.player.pos, end, shot=True): return 0.
        return max(0., 1-miss/(margin+8))*(1 if build.calibrated else .8)

    def propose(self, s, build, profile, target, velocity, geometry, aim=None):
        aim = aim or PredictiveAim(profile.aim.params)
        proposals = []
        for direction in self.directions:
            hit = self.hit_score(s, build, aim, target, direction, velocity, geometry)
            if hit >= self.params["min_hit_score"] and hit > 0:
                proposals.append(AttackProposal(direction, hit, hit*float(s.stats.get("damage", 3.5))/build.fire_interval, "attack_window"))
        # Ordinary tears have no resource cost: never withhold a valid shot.
        return proposals or [AttackProposal((0, 0), 0, 0, "no_valid_firing_lane")]


EXECUTABLES = Registry()
for name, factory in {
    "target.threat_first": ThreatFirst, "target.finish_first": FinishFirst, "target.weighted": WeightedTarget,
    "position.keep_range": KeepRange, "position.kite": Kite, "position.strafe": Strafe,
    "aim.direct": DirectAim, "aim.predictive": PredictiveAim, "fire.normal_tears": NormalTears,
    "evade.max_clearance": MaxClearance, "evade.minimal_shift": MinimalShift,
    "escape.keep_routes": KeepRoutes, "recovery.local_repath": LocalRepath,
}.items(): EXECUTABLES.register(name, factory)
