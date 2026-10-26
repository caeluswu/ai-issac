"""Versioned combat contracts. Units: pixels and game updates (30 Hz)."""
from __future__ import annotations

import math
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    friendly: bool
    dead: bool
    collision_class: int
    can_shut_doors: bool = True  # Missing legacy evidence remains blocking.


class WorldSnapshot(Observed):
    schema_version: Literal[1]
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

    @model_validator(mode="before")
    @classmethod
    def lua_tables(cls, value):
        d = dict(value)
        for k in ("enemies", "bombs", "hazards"):
            if d.get(k) == {}: d[k] = []
        if d.get("movement_inheritance") == []: d["movement_inheritance"] = {}
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
        return self

    @property
    def hostiles(self): return [e for e in self.enemies if not e.friendly and not e.dead and e.hp > 0]
    @property
    def blocking_combat(self): return self.room["enemy_count"] > 0 or any(e.can_shut_doors for e in self.hostiles)
    @property
    def hp(self): return int(self.health["red_hearts"])+int(self.health["soul_hearts"])
    @property
    def snapshot_id(self): return f"{self.room_visit_id}:{self.frame}"


class ContextRef(Strict):
    run_id: str
    room_visit_id: str
    snapshot_id: str
    build_revision: int = 1
    catalog_revision: int = Field(default=2, ge=1, le=2)
    plan_revision: int = 0


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
    revision: int = 1
    move_speed: float = 4.2
    player_displacement_scale: float = Field(default=1, gt=0, le=4)
    tear_speed: float = 10.0
    effective_range: float = 220
    fire_interval: float = 11
    calibrated: bool = False
    calibration_samples: int = 0
    inheritance_available: bool = False


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
