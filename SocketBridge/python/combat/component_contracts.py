"""Pure component DTOs and host limits, shared by trusted modules and validators.

Coordinates are pixels, time is game updates. These contracts do not provide
code isolation; only trusted host components may run directly in the solver.
"""
from dataclasses import dataclass
from typing import Protocol
from .models import Vec


@dataclass(frozen=True)
class ComponentLimits:
    max_attack_proposals: int = 16
    max_waypoints: int = 256
    max_coordinate_abs: float = 100_000.
    max_footprint_width: float = 4096.
    max_score_abs: float = 1_000_000_000.
    max_wait_frames: int = 90


HOST_LIMITS = ComponentLimits()


@dataclass(frozen=True)
class CapabilitySet:
    family: str
    moving_attack: bool = False
    break_plain_poop: bool = False


@dataclass(frozen=True)
class ProposalContext:
    run_id: str
    room_visit_id: str
    build_revision: int
    catalog_revision: int | str
    observed_frame: int
    valid_until_frame: int
    floor_instance_id: str | None = None
    experience_revision: int = 0


@dataclass(frozen=True)
class MotionProposal:
    waypoints: tuple[Vec, ...]
    reason: str = "weapon_attack_geometry"


@dataclass(frozen=True)
class WaitEvidence:
    token: str
    reason: str
    deadline_frame: int
    evidence: str


@dataclass(frozen=True)
class AttackProposal:
    shoot: tuple[int, int]
    hit_score: float
    # Heuristic cumulative damage over prediction_horizon_frames, not DPS
    # or a calibrated probability. The host normalizes candidate horizons.
    expected_damage: float
    reason: str
    wait: WaitEvidence | None = None
    target_id: str | None = None
    footprint: tuple[float, float, float, float, float] | None = None
    context: ProposalContext | None = None
    prediction_horizon_frames: int = 30
    # Withdrawing a charged release may require continuing the observed hold.
    # Zero input can itself fire. Trusted weapon adapters supply this action.
    withdrawal_shoot: tuple[int, int] = (0, 0)
    position_bias: float = 0.


class WeaponAdapter(Protocol):
    capabilities: CapabilitySet

    def propose(self, s, build, profile, target, velocity, geometry, aim=None) -> list[AttackProposal]: ...


class FrameObserver(Protocol):
    """Optional hook: observe once/update; propose must not commit actions."""
    def begin_frame(self, snapshot, build) -> None: ...


class FiringRouteProvider(Protocol):
    def firing_route(self, geometry, start, target, build, excluded=(), terrain=False) -> list[Vec]: ...
