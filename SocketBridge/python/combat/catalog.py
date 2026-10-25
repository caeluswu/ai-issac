"""Executable registry: closed names, per-slot schemas and bounded parameters."""
from dataclasses import dataclass
from .models import BattlePlan, ContextRef
from .components import EXECUTABLES


@dataclass(frozen=True)
class ComponentSpec:
    name: str
    parameters: dict
    description: str
    inputs: tuple[str, ...]
    weapons: tuple[str, ...] = ("normal_tears",)
    max_duration_frames: int = 90
    state_scope: str = "room_and_profile"
    failure_reasons: tuple[str, ...] = ("missing_input", "incompatible_capability", "no_feasible_proposal")


# Each parameter is (default, minimum, maximum) or (default, choices).
TARGET = {"switch_margin": (.15, .05, .5), "min_hold_ticks": (10, 0, 30)}
POSITION = {"min_range_ratio": (.45, .2, .95), "max_range_ratio": (.8, .2, .95)}
CATALOG = {}


def register(name, parameters, description, inputs):
    CATALOG[name] = ComponentSpec(name, parameters, description, inputs,
                                 weapons=("normal_tears",) if name.startswith(("aim.", "fire.")) else ("any",))


for name in ("threat_first", "finish_first", "weighted"):
    p = dict(TARGET)
    if name == "weighted": p.update(threat_weight=(.5, 0, 1), finish_weight=(.3, 0, 1), approach_cost_weight=(.2, 0, 1))
    register("target."+name, p, name, ("enemies", "player", "geometry"))
for name in ("keep_range", "kite", "strafe"):
    p = dict(POSITION)
    if name == "strafe": p.update(preferred_side=("auto", ("auto", "left", "right")), side_switch_ticks=(12, 6, 30))
    register("position."+name, p, name, ("target", "build", "geometry"))
for name in ("direct", "predictive"):
    register("aim."+name, {"max_lead_ticks": (12, 3, 24), "movement_inheritance": (True, (True, False))}, name, ("target", "build"))
register("fire.normal_tears", {"min_hit_score": (.2, 0, .8)}, "cardinal tear fire", ("target", "geometry", "build"))
for name in ("minimal_shift", "max_clearance"):
    register("evade."+name, {"extra_margin_px": (4, 0, 12)}, name, ("enemies", "projectiles", "bombs", "hazards"))
register("escape.keep_routes", {"horizon_ticks": (30, 15, 45), "min_safe_area_ratio": (.25, .1, .5)}, "future reachable space", ("geometry", "enemies"))
register("recovery.local_repath", {"stall_window_ticks": (30, 15, 60), "recovery_ticks": (6, 3, 12)}, "stall-triggered detour", ("position_history", "geometry"))


def compile_plan(value, context: ContextRef, capabilities=("normal_tears",)) -> BattlePlan:
    if isinstance(value, BattlePlan): value = value.model_dump()
    if value.get("schema_version", 1) == 1:
        value = {**value, "schema_version": 2, "required_capabilities": ["normal_tears"]}
    plan = BattlePlan.model_validate(value)
    if not set(plan.required_capabilities).issubset(capabilities): raise ValueError("Unsupported build capability")
    a, b = plan.context_ref.model_dump(), context.model_dump()
    a.pop("snapshot_id"); b.pop("snapshot_id")
    if a != b: raise ValueError("context_ref: stale run, room, build, catalog or plan revision")
    if plan.default_profile not in plan.profiles: raise ValueError("default_profile: unknown profile")
    for rule in plan.switch_rules:
        if rule.use_profile not in plan.profiles: raise ValueError("switch_rules.use_profile: unknown profile")
    for profile_name, profile in plan.profiles.items():
        for slot in type(profile).model_fields:
            choice = getattr(profile, slot)
            spec = CATALOG.get(choice.component)
            path = f"profiles.{profile_name}.{slot}"
            if not spec or not choice.component.startswith(slot+"."):
                raise ValueError(f"{path}.component: unregistered component")
            if choice.component not in EXECUTABLES.factories:
                raise ValueError(f"{path}.component: missing executable implementation")
            if "any" not in spec.weapons and not set(spec.weapons).intersection(capabilities):
                raise ValueError(f"{path}.component: incompatible weapon capability")
            if set(choice.params)-set(spec.parameters): raise ValueError(f"{path}.params: unknown parameters")
            for name, bounds in spec.parameters.items():
                value = choice.params.setdefault(name, bounds[0])
                valid = False
                if len(bounds) == 2:
                    valid = any(type(value) is type(x) and value == x for x in bounds[1])
                elif type(value) in (float, int):
                    valid = bounds[1] <= value <= bounds[2]
                    if name.endswith("ticks"): valid &= type(value) is int
                if not valid: raise ValueError(f"{path}.params.{name}: allowed {bounds[1:]}")
            if slot == "position" and choice.params["min_range_ratio"] >= choice.params["max_range_ratio"]:
                raise ValueError(f"{path}.params: min_range_ratio must be below max_range_ratio")
            if choice.component == "target.weighted" and abs(sum(choice.params[x] for x in ("threat_weight", "finish_weight", "approach_cost_weight"))-1) > 1e-6:
                raise ValueError(f"{path}.params: weights must sum to one")
    return plan


def default_plan(context: ContextRef):
    choices = ("target.threat_first", "position.kite", "aim.predictive", "fire.normal_tears", "evade.max_clearance", "escape.keep_routes", "recovery.local_repath")
    return compile_plan({"schema_version": 2, "context_ref": context.model_dump(), "default_profile": "safe",
        "profiles": {"safe": {x.split('.')[0]: {"component": x, "params": {}} for x in choices}},
        "switch_rules": [], "replan_on": ["confirmed_damage", "no_progress", "new_enemy_mechanism", "repeated_recovery", "invalid_profile"],
        "rationale": {"summary": "Local deterministic fallback; not an LLM decision", "evidence_refs": []}}, context)


def specs():
    return {n: {"parameters": s.parameters, "description": s.description, "inputs": s.inputs,
                "weapons": s.weapons, "max_duration_frames": s.max_duration_frames,
                "state_scope": s.state_scope, "failure_reasons": s.failure_reasons} for n, s in CATALOG.items()}
