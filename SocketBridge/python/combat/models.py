"""Versioned combat contracts. Units: pixels and game updates (30 Hz)."""
from __future__ import annotations

import math
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Vec(Strict):
    x: float
    y: float

    def __add__(self, b): return Vec(x=self.x+b.x, y=self.y+b.y)
    def __sub__(self, b): return Vec(x=self.x-b.x, y=self.y-b.y)
    def __mul__(self, n): return Vec(x=self.x*n, y=self.y*n)
    def length(self): return math.hypot(self.x, self.y)
    def distance(self, b): return (self-b).length()
    def dot(self, b): return self.x*b.x+self.y*b.y
    def unit(self): return self*(1/max(1e-9, self.length()))


ZERO = Vec(x=0, y=0)


class Observed(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)


class Player(Observed):
    pos: Vec
    vel: Vec
    collision_radius: float = Field(gt=0)
    dead: bool


class Enemy(Observed):
    instance_id: str
    type: int
    variant: int
    pos: Vec
    vel: Vec
    hp: float
    max_hp: float
    collision_radius: float = Field(ge=0)
    vulnerable: bool
    damage_directions: dict[str, bool] | None = None
    friendly: bool
    dead: bool
    collision_class: int
    is_boss: bool | None = None
    can_shut_doors: bool = True  # Missing legacy evidence remains blocking.
    subtype: int | None = None
    state: int | None = None
    state_frame: int | None = None
    contact_damage: float | None = None
    v1: Vec | None = None
    v2: Vec | None = None


class WeaponObservation(Strict):
    status: Literal["known", "partial", "unknown"]
    types: list[int] = Field(max_length=64)
    unknowns: list[str]


class EffectObservation(Strict):
    kind: Literal["collectible", "trinket", "null", "unknown"]
    item_id: int | None
    count: int = Field(ge=0)
    cooldown: int


class TemporaryEffectsObservation(Strict):
    status: Literal["known", "partial", "unknown"]
    items: list[EffectObservation] = Field(max_length=256)
    unknowns: list[str]


class InventoryCoverage(Strict):
    static_status: Literal["known", "partial", "unknown"]
    catalog_max_id: int | None = Field(default=None, ge=0)
    dynamic_status: Literal["unknown", "partial"]
    observed_dynamic_ids: list[int]
    unknowns: list[str]


class FloorObservation(Strict):
    instance_id: str = Field(min_length=1)
    generation: int = Field(ge=0)
    stage: int = Field(ge=0)
    stage_type: int = Field(ge=0)
    dimension: int | None = Field(default=None, ge=0, le=2)
    room_list_index: int | None = None
    unknowns: list[str]


class PickupObservation(Observed):
    id: int
    instance_id: str = Field(min_length=1)
    variant: int
    sub_type: int | None = None
    pos: Vec
    vel: Vec
    collision_radius: float = Field(ge=0)
    price: int | None = None  # negative prices represent health deals
    shop_item_id: int | None = None
    wait: int | None = None
    options_pickup_index: int | None = None
    identity_visibility: Literal["visible", "hidden", "unknown"]
    identity_reason: str
    is_empty: bool | None = None

    @model_validator(mode="after")
    def no_hidden_collectible_identity(self):
        if self.variant == 100 and self.identity_visibility != "visible" and self.sub_type is not None:
            raise ValueError("Unverified collectible identity must be redacted")
        return self


class HealthObservation(Strict):
    dead: bool
    red_hearts: int = Field(ge=0)
    max_hearts: float = Field(ge=0)
    soul_hearts: int = Field(ge=0)
    black_hearts: int = Field(ge=0)  # API bitmask, not another HP count
    bone_hearts: int = Field(ge=0)
    golden_hearts: int = Field(ge=0)
    eternal_hearts: int = Field(ge=0)
    rotten_hearts: int = Field(ge=0)
    broken_hearts: int = Field(ge=0)
    extra_lives: int = Field(ge=0)


class FloorProgress(Strict):
    capability_version: Literal[1]
    curses: int | None = Field(default=None, ge=0)
    can_pick_red: bool | None = None
    can_pick_soul: bool | None = None
    controls_enabled: bool | None = None
    extra_animation_finished: bool | None = None


class ExitObservation(Strict):
    instance_id: str
    grid_index: int
    type: int
    variant: int
    state: int
    collision: int
    pos: Vec


class WorldSnapshot(Observed):
    schema_version: Literal[1, 2]
    run_id: str
    room_visit_id: str
    frame: int = Field(ge=0)
    paused: bool
    mode: Literal["MANUAL", "AUTO", "FORCE_AI"]
    room: dict[str, Any]
    layout: dict[str, Any]
    player: Player
    stats: dict[str, Any]
    health: dict[str, Any]
    inventory: dict[str, Any]
    enemies: list[Enemy]
    projectiles: dict[str, Any]
    bombs: list[dict]
    hazards: list[dict]
    movement_inheritance: dict[str, dict]
    seed: str
    applied_action_id: int | None = None
    applied_frame: int | None = None
    weapon_feedback: dict[str, Any] | None = None
    pickups: list[PickupObservation] | None = None
    weapons: WeaponObservation | None = None
    effects: TemporaryEffectsObservation | None = None
    inventory_coverage: InventoryCoverage | None = None
    floor: FloorObservation | None = None
    health_observation: HealthObservation | None = None
    floor_progress: FloorProgress | None = None
    exit_observations: list[ExitObservation] | None = None

    @model_validator(mode="before")
    @classmethod
    def lua_tables(cls, value):
        d = dict(value)
        for k in ("enemies", "bombs", "hazards", "pickups", "exit_observations"):
            if d.get(k) == {}: d[k] = []
        if d.get("movement_inheritance") == []: d["movement_inheritance"] = {}
        for key in ("weapons", "effects", "inventory_coverage"):
            if isinstance(d.get(key), dict):
                group = dict(d[key])
                for name in ("types", "items", "unknowns", "observed_dynamic_ids"):
                    if group.get(name) == {}: group[name] = []
                d[key] = group
        if isinstance(d.get("floor"), dict) and d["floor"].get("unknowns") == {}:
            d["floor"] = {**d["floor"], "unknowns": []}
        return d

    @model_validator(mode="after")
    def required_facts(self):
        for group, keys in ((self.room, ("room_idx", "is_clear", "enemy_count", "stage", "difficulty")),
                            (self.stats, ("player_type", "weapon_type", "speed", "tear_range", "shot_speed", "can_fly")),
                            (self.health, ("red_hearts", "soul_hearts", "bone_hearts")),
                            (self.layout, ("cells", "doors", "grid", "width")),
                            (self.projectiles, ("enemy_projectiles", "player_tears", "lasers"))):
            if any(k not in group for k in keys):
                raise ValueError(f"Missing critical observation: {keys}")
        if not self.layout["cells"]: raise ValueError("Empty room geometry")
        if self.schema_version == 2:
            for name in ("pickups", "weapons", "effects", "inventory_coverage", "floor", "health_observation"):
                if getattr(self, name) is None: raise ValueError("Missing V2 observation: "+name)
            if not self.floor.instance_id.startswith(self.run_id+":floor:"):
                raise ValueError("Floor belongs to a different run")
            if (self.floor.stage, self.floor.stage_type) != (self.room["stage"], self.room.get("stage_type")):
                raise ValueError("Inconsistent floor observation")
            if self.health_observation.dead != self.player.dead:
                raise ValueError("Inconsistent life observation")
            ordinary = self.weapons.status == "known" and self.weapons.types == [1]
            if self.stats["weapon_type"] != (1 if ordinary else -1):
                raise ValueError("Inconsistent legacy weapon observation")
            for name in type(self.health_observation).model_fields:
                if name != "dead" and self.health.get(name) != getattr(self.health_observation, name):
                    raise ValueError("Inconsistent health observation: "+name)
        return self

    @property
    def hostiles(self): return [e for e in self.enemies if not e.friendly and not e.dead and e.hp > 0]
    @property
    def blocking_combat(self): return self.room["enemy_count"] > 0 or any(e.can_shut_doors for e in self.hostiles)
    @property
    def hp(self): return int(self.health["red_hearts"])+int(self.health["soul_hearts"])
    @property
    def alive(self): return not self.player.dead
    @property
    def snapshot_id(self): return f"{self.room_visit_id}:{self.frame}"


class ContextRef(Strict):
    run_id: str
    room_visit_id: str
    snapshot_id: str
    build_revision: int = 1
    catalog_revision: int | str = 2
    plan_revision: int = 0
    floor_instance_id: str | None = None
    experience_revision: int = Field(default=0, ge=0)

    @field_validator("catalog_revision", mode="before")
    @classmethod
    def catalog_type(cls, value):
        if type(value) not in (int, str): raise ValueError("Catalog revision must be integer or string")
        return value

    @model_validator(mode="after")
    def version_identity(self):
        revision = self.catalog_revision
        if isinstance(revision, int):
            if revision < 1: raise ValueError("Invalid catalog revision")
        elif not revision.strip() or len(revision) > 128: raise ValueError("Invalid catalog revision")
        return self


class ComponentChoice(Strict):
    component: str
    params: dict[str, Any] = Field(default_factory=dict)


class TacticProfile(Strict):
    target: ComponentChoice
    position: ComponentChoice
    aim: ComponentChoice
    fire: ComponentChoice
    evade: ComponentChoice
    escape: ComponentChoice
    recovery: ComponentChoice


class Predicate(Strict):
    field: Literal["attackable_count", "health_ratio", "target_range_ratio", "risk", "enemy_type_count"]
    op: Literal["lt", "le", "gt", "ge", "eq"]
    value: float = Field(ge=0, le=10000)
    enemy_type: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def selector(self):
        if self.field == "enemy_type_count" and self.enemy_type is None:
            raise ValueError("enemy_type_count requires enemy_type")
        return self


class SwitchRule(Strict):
    when: list[Predicate] = Field(min_length=1, max_length=4)
    use_profile: str


class Rationale(Strict):
    summary: str = Field(max_length=2000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)


class BattlePlan(Strict):
    schema_version: Literal[1, 2] = 2
    context_ref: ContextRef
    default_profile: str
    profiles: dict[str, TacticProfile] = Field(min_length=1, max_length=3)
    switch_rules: list[SwitchRule] = Field(default_factory=list, max_length=8)
    replan_on: list[Literal["confirmed_damage", "no_progress", "new_enemy_mechanism", "repeated_recovery", "invalid_profile"]]
    rationale: Rationale
    required_capabilities: list[str] = Field(default_factory=lambda: ["normal_tears"], max_length=8)


class EnemyGroup(Strict):
    mechanism: str
    entity_refs: list[str]
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str]


class Danger(Strict):
    kind: str
    evidence_refs: list[str]


class BattleAssessment(Strict):
    context_ref: ContextRef
    enemy_groups: list[EnemyGroup]
    primary_dangers: list[Danger]
    terrain_constraints: list[str]
    weapon_opportunities: list[str]
    recommended_tactics: list[str]
    unknowns: list[str]


class BuildProfile(Strict):
    family: str = "normal_tears"
    modifiers: list[str] = Field(default_factory=list)
    attack_binding: str | None = None
    calibration_state: Literal["ready","needs_probe","estimated"] = "ready"
    revision: int = 1
    move_speed: float = 4.2
    player_displacement_scale: float = Field(default=1, gt=0, le=4)
    tear_speed: float = 10.0
    effective_range: float = 220
    fire_interval: float = 11
    calibrated: bool = False
    calibration_observations: list[dict] = Field(default_factory=list, max_length=60)
    calibration_samples: int = 0
    inheritance_available: bool = False
    signature: str = ""
    calibration_key: str = ""
    changed_domains: list[str] = Field(default_factory=list)
    damage_per_hit: float = Field(default=3.5, ge=0, le=1000000)
    prediction_horizon_frames: int = Field(default=30, ge=1, le=120)
    experience_revision: int = Field(default=0, ge=0)
    calibration_source: Literal["prior", "observed", "experience"] = "prior"


class ActionFrame(Strict):
    frame: int
    room_visit_id: str
    move: tuple[int, int]
    shoot: tuple[int, int]
    profile: str
    target: str | None
    state: str
    score: dict[str, float]
    decision_ms: float
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class ActionReceipt(Strict):
    action_id: int
    accepted: bool
    frame: int
    room_visit_id: str


class RoomReport(Strict):
    result: str
    success: bool
    llm_configured: bool
    completed_door_transition: bool
    damage_half_hearts: int
    lost_bone_hearts: int
    remaining_half_hearts: int | None
    combat_ticks: int
    wall_seconds: float
    pause_seconds: float
    sent_actions: int
    accepted_actions: int
    observed_applied_actions: int
    fallback_count: int
    recovery_count: int
    decision_p95_ms: float | None
    seed: str | None
    error: str | None
    progress_metrics: dict[str, Any] = Field(default_factory=dict)
    replan_requests: int = 0
    decision_p99_ms: float | None = None
    safe_clear_ticks: int = 0
    discarded_decisions: int = 0
    contract_override_count: int = 0
    damage_scope: str = "task"
    combat_damage_half_hearts: int = 0
    combat_scope: str = "first_observed_blocking_room"
    task_ticks: int = 0
    environment_ticks: int = 0
    navigation_replan_requests: int = 0
    navigation_decision_p95_ms: float | None = None
    navigation_decision_p99_ms: float | None = None
    navigation_metrics: list[dict[str, Any]] = Field(default_factory=list)
    voluntary_health_spend: int = 0
    health_container_loss: int = 0
    resources_spent: dict[str, int] = Field(default_factory=dict)
