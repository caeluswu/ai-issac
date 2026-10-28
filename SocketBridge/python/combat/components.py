"""Replaceable pure policy and weapon modules; no IO or controller references."""
import math
from .models import Vec, ZERO
from .component_contracts import (
    CapabilitySet, MotionProposal, AttackProposal, WaitEvidence, WeaponAdapter, ProposalContext, HOST_LIMITS,
)
from .component_validation import validate_result


class Registry:
    def __init__(self): self.factories = {}
    def register(self, name, factory):
        if name in self.factories: raise ValueError("Duplicate executable component: "+name)
        self.factories[name] = factory
    def create(self, choice):
        if choice.component not in self.factories: raise ValueError("Missing executable component: "+choice.component)
        return self.factories[choice.component](dict(choice.params))


class ResilientComponent:
    """Quarantine returned-value faults in trusted modules; not code isolation."""
    def __init__(self, primary, fallback, identity, family="normal_tears", max_wait_frames=90):
        self.primary, self.fallback, self.identity = primary, fallback, identity
        self.family = family
        self.max_wait_frames = min(max_wait_frames, HOST_LIMITS.max_wait_frames)
        self.broken = False
        self.failure = None
        self.context = self.snapshot = None
        self.last_observation = None
    def bind(self, snapshot, build, ref):
        self.snapshot = snapshot
        self.context = ProposalContext(snapshot.run_id, snapshot.room_visit_id, build.revision,
                                       ref.catalog_revision, snapshot.frame, snapshot.frame, ref.floor_instance_id, ref.experience_revision)
    def fail(self, method, exc):
        self.broken = True
        self.failure = {"component": self.identity, "method": method, "error": type(exc).__name__+": "+str(exc)}
    def checked(self, instance, method, args, kwargs):
        value = getattr(instance, method)(*args, **kwargs)
        if self.context is None: raise ValueError("component has no bound execution context")
        return validate_result(method, value, self.context, self.snapshot, self.max_wait_frames)
    def __getattr__(self, name):
        instance = self.fallback if self.broken else self.primary
        value = getattr(instance, name)
        if not callable(value):
            if name == "capabilities" and (not isinstance(value, CapabilitySet) or value.family != self.family):
                self.fail(name, ValueError("incompatible weapon capability declaration"))
                return self.fallback.capabilities
            return value
        def guarded(*args, **kwargs):
            if name == "begin_frame":
                if self.last_observation == self.context: return None
                self.last_observation = self.context
            try: return self.checked(self.fallback if self.broken else self.primary, name, args, kwargs)
            except Exception as exc:
                if self.broken: raise RuntimeError("COMPATIBLE_FALLBACK_FAILED: "+self.identity) from exc
                self.fail(name, exc)
                replacement = getattr(self.fallback, name, None)
                return self.checked(self.fallback, name, args, kwargs) if replacement else None
        return guarded
    def pop_failure(self):
        failure, self.failure = self.failure, None
        return failure


DEFAULT_IDS = {"target": "target.threat_first", "position": "position.kite", "aim": "aim.predictive",
               "fire": "fire.normal_tears", "evade": "evade.max_clearance", "escape": "escape.keep_routes",
               "recovery": "recovery.local_repath"}


class NoFire:
    """Neutral inputs for an unsupported family; release may still fire it."""
    def __init__(self, family):
        self.capabilities = CapabilitySet(family)
    def propose(self, *args): return [AttackProposal((0, 0), 0, 0, "no_compatible_fire_fallback")]
    def firing_route(self, *args, **kwargs): return []
    def pending_damage(self, *args): return 0.


def resilient_component(choice, slot, registry, family="normal_tears"):
    from .catalog import CATALOG
    name = DEFAULT_IDS[slot]
    fallback = EXECUTABLES.factories[name]({k: v[0] for k, v in CATALOG[name].parameters.items()})
    if slot == "fire" and family != "normal_tears": fallback = NoFire(family)
    try: primary = registry.create(choice)
    except Exception as exc:
        wrapped = ResilientComponent(fallback, fallback, choice.component, family)
        wrapped.fail("factory", exc)
        return wrapped
    wrapped = ResilientComponent(primary, fallback, choice.component, family,
                                 getattr(CATALOG.get(choice.component), "max_duration_frames", 90))
    declared = getattr(primary, "capabilities", None)
    if slot == "fire" and declared is not None and (not isinstance(declared, CapabilitySet) or declared.family != family):
        wrapped.fail("capabilities", ValueError("incompatible weapon capability declaration"))
    return wrapped


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
    def __init__(self, params): self.params, self.relocation_goal = params, None
    def route(self, geometry, s, target, build, weapon, excluded=(), terrain=False):
        provider = getattr(weapon, "firing_route", None)
        result = MotionProposal(tuple(provider(geometry, s.player.pos, target, build, excluded, terrain))) if provider else MotionProposal((), "no_weapon_route_provider")
        if result.waypoints: self.relocation_goal = None
        return result
    def score(self, delta, desired, s, build):
        low, high = (self.params[x]*build.effective_range for x in ("min_range_ratio", "max_range_ratio"))
        return -max(low-delta.length(), 0, delta.length()-high)/50-min(abs(delta.x), abs(delta.y))/90
    def reposition(self, geometry, s, target, build, excluded=()):
        goal = self.relocation_goal
        if goal and s.player.pos.distance(goal) > 20 and (round(goal.x), round(goal.y)) not in excluded:
            route = geometry.path(s.player.pos, goal)
        else:
            route = geometry.reposition_route(s.player.pos, target, excluded)
            self.relocation_goal = route[-1] if route else None
        return MotionProposal(tuple(route), "occlusion_reposition")


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
    def score(self, fraction, slack):
        return min(.7, fraction)*.8-(2 if fraction < self.params["min_safe_area_ratio"] else 0)+min(1., max(0., slack)/self.params["horizon_ticks"])*.1


class LocalRepath:
    def __init__(self, params): self.params = params
    def reject_goal(self, frame, level, last_recovery):
        return bool(level and frame-last_recovery >= max(15, self.params["recovery_ticks"]*7.5))


class NormalTears:
    capabilities = CapabilitySet("normal_tears", moving_attack=True, break_plain_poop=True)
    directions = ((1, 0), (-1, 0), (0, 1), (0, -1))
    def __init__(self, params): self.params = params
    def begin_frame(self, snapshot, build):
        self.target_radii = {(e.pos.x,e.pos.y):e.collision_radius+5 for e in snapshot.hostiles}
    def firing_route(self, geometry, start, target, build, excluded=(), terrain=False):
        radius = getattr(self,'target_radii',{}).get((target.x,target.y),0.) if not terrain else 0.
        return geometry.firing_route(start, target, build.effective_range, terrain=terrain, excluded=excluded,target_radius=radius)
    def pending_damage(self, s, enemy):
        damage = 0.
        for tear in s.projectiles["player_tears"] or []:
            position, velocity = Vec(**tear["pos"]), Vec(**tear["vel"])
            delta = enemy.pos-position
            ticks = delta.dot(velocity)/max(1e-9, velocity.dot(velocity))
            if 0 <= ticks <= 20 and (position+velocity*ticks).distance(enemy.pos+enemy.vel*ticks) <= enemy.collision_radius+tear.get("collision_radius", 5):
                damage += float(s.stats.get("damage", 3.5))*.6
        return damage
    def launch_velocity(self, s, build, aim, shot, velocity):
        launch = Vec(x=shot[0], y=shot[1])*build.tear_speed
        if aim.params["movement_inheritance"]:
            measured = s.movement_inheritance.get(f"{shot[0]},{shot[1]}")
            launch += (Vec(**measured)+(velocity-s.player.vel)*.5) if measured else velocity*.5
        return launch

    def hit_score(self, s, build, aim, target, shot, velocity, geometry):
        if target is None or not target.vulnerable: return 0.
        launch = self.launch_velocity(s, build, aim, shot, velocity)
        from .directional_armor import damage_from_velocity
        if damage_from_velocity(target, launch) is False: return 0.
        enemy_velocity = aim.velocity(target)
        relative = launch-enemy_velocity
        delta = target.pos-s.player.pos
        ticks = delta.dot(relative)/max(1e-9, relative.dot(relative))
        forecast_ticks = min(max(0, ticks), aim.params["max_lead_ticks"])
        if ticks > forecast_ticks and enemy_velocity.length() > 0:
            delta = target.pos+enemy_velocity*forecast_ticks-s.player.pos
            ticks = delta.dot(launch)/max(1e-9, launch.dot(launch))
        launch_length = launch.length()
        if ticks < 0: return 0.
        hit = s.player.pos+launch*ticks
        miss = hit.distance(target.pos+enemy_velocity*min(ticks, aim.params["max_lead_ticks"]))
        contact_back = math.sqrt(max(0,(target.collision_radius+5)**2-miss**2))
        miss += enemy_velocity.length()*max(0, ticks-forecast_ticks)*.2
        margin = target.collision_radius+5
        # Stop at first projected contact, not at a center embedded in a wall.
        # Intervening solid terrain still blocks the entire incoming segment.
        back = min(22 if target.instance_id.startswith("terrain:") else contact_back,ticks*launch_length)
        if ticks*launch_length-back > build.effective_range: return 0.
        scale = back/max(1e-9,launch_length)
        end = Vec(x=hit.x-launch.x*scale,y=hit.y-launch.y*scale)
        if not geometry.line_clear(s.player.pos, end, shot=True): return 0.
        return max(0., 1-miss/(margin+8))*(1 if build.calibrated else .8)

    def propose(self, s, build, profile, target, velocity, geometry, aim=None):
        aim = aim or PredictiveAim(profile.aim.params)
        proposals = []
        pool = [target] if target else []
        # Bound opportunity search, not threat perception. Every dangerous entity
        # remains in the safety field; firing still has at most four proposals.
        secondary = [e for e in s.hostiles if e.vulnerable and (target is None or e.instance_id != target.instance_id)
                     and e.pos.distance(s.player.pos) <= build.effective_range+e.collision_radius+5]
        pool += sorted(secondary, key=lambda e: (e.pos.distance(s.player.pos), e.instance_id))[:7]
        for direction in self.directions:
            choices = []
            for enemy in pool:
                hit = self.hit_score(s, build, aim, enemy, direction, velocity, geometry)
                if hit >= self.params["min_hit_score"] and hit > 0:
                    primary = target is not None and enemy.instance_id == target.instance_id
                    choices.append((hit*(1 if primary else .95), enemy, primary))
            if choices:
                hit, enemy, primary = max(choices, key=lambda item: (item[0], item[1].instance_id))
                end = s.player.pos+self.launch_velocity(s, build, aim, direction, velocity).unit()*build.effective_range
                footprint = (s.player.pos.x, s.player.pos.y, end.x, end.y, 16.)
                reward = hit*build.damage_per_hit*build.prediction_horizon_frames/build.fire_interval
                proposals.append(AttackProposal(direction, hit, reward,
                    "attack_window" if primary else "opportunistic_secondary", target_id=enemy.instance_id, footprint=footprint,
                    prediction_horizon_frames=build.prediction_horizon_frames))
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
