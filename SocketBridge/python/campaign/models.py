from dataclasses import dataclass
from typing import Literal
from pydantic import Field
from combat.models import Strict, ContextRef, Vec


@dataclass(frozen=True)
class RoomKey:
    floor: str
    dimension: int
    index: int

    @classmethod
    def from_snapshot(cls, s):
        if not s.floor or s.floor.dimension is None or s.floor.room_list_index is None:
            raise ValueError("FLOOR_IDENTITY_UNKNOWN")
        return cls(s.floor.instance_id,s.floor.dimension,s.floor.room_list_index)

    def text(self): return f"{self.floor}:{self.dimension}:{self.index}"


class FloorContext(Strict):
    run_id: str
    floor_instance_id: str
    stage: int
    stage_type: int
    dimension: int
    curses: int
    support_profile: str = "j835-isaac-normal-tears-first-floor-v1"
    initial_snapshot_id: str


class DoorObservation(Strict):
    slot: int
    target_room: int
    target_list_index: int | None = None
    target_room_type: int
    is_open: bool
    is_locked: bool
    x: float
    y: float


class FloorObjective(Strict):
    objective_id: str
    context_ref: ContextRef
    kind: Literal["enter_room","pickup","leave_floor"]
    target: dict
    deadline_frame: int
    cost_keys: int = Field(default=0,ge=0,le=1)


class TransitionReceipt(Strict):
    intent_id: str
    source: str
    target: str
    slot: int | None = None
    from_visit: str
    to_visit: str
    applied_action_id: int | None = None
    keys_spent: int = 0
    matched_expected: bool
    completed_frame: int


class RoomResult(Strict):
    room_key: str
    room_visit_id: str
    combat_result: str
    post_clear_result: str
    terminal_snapshot_id: str
    alive: bool
    actionable: bool
    damage: int
    result: str
    build_revision: int | None = None
    experience_revision: int | None = None


class FloorReport(Strict):
    result: str
    success: bool
    error: str | None = None
    seed: str | None = None
    context: FloorContext | None = None
    rooms: list[dict] = Field(default_factory=list)
    transitions: list[TransitionReceipt] = Field(default_factory=list)
    evidence: dict = Field(default_factory=dict)
    damage_half_hearts: int = 0
    bones_lost: int = 0
    resources_spent: dict = Field(default_factory=dict)
    game_ticks: int = 0
    wall_seconds: float = 0
    process_wall_seconds: float = 0
    evaluation_mode: str = "normal_random"
    pause_seconds: float = 0
    llm_configured: bool = False
    fallback_count: int = 0
    request_budget: dict = Field(default_factory=dict)
    cleanup_errors: list[str] = Field(default_factory=list)
