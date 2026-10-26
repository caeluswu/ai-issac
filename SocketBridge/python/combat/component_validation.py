"""Validate returned values before they can influence the trusted solver."""
from dataclasses import replace
import math
from .component_contracts import HOST_LIMITS, AttackProposal, MotionProposal, WaitEvidence, ProposalContext
from .models import Vec


def finite(value, low=-HOST_LIMITS.max_score_abs, high=HOST_LIMITS.max_score_abs):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("invalid finite numeric result")
    return value


def vector(value):
    if not isinstance(value, Vec): raise ValueError("expected Vec")
    for number in (value.x, value.y):
        finite(number, -HOST_LIMITS.max_coordinate_abs, HOST_LIMITS.max_coordinate_abs)
    return value


def path(value):
    if not isinstance(value, (tuple, list)) or len(value) > HOST_LIMITS.max_waypoints:
        raise ValueError("invalid or oversized path")
    return tuple(vector(p) for p in value)


def validate_result(method, value, context, snapshot, max_wait_frames):
    if method == "propose":
        if not isinstance(value, (tuple, list)) or not 1 <= len(value) <= HOST_LIMITS.max_attack_proposals:
            raise ValueError("empty, oversized or invalid attack proposals")
        result = []
        for attack in value:
            if not isinstance(attack, AttackProposal): raise ValueError("expected AttackProposal")
            if (type(attack.shoot) is not tuple or len(attack.shoot) != 2 or
                    any(type(x) is not int or x not in (-1, 0, 1) for x in attack.shoot) or
                    sum(abs(x) for x in attack.shoot) > 1):
                raise ValueError("invalid cardinal shoot direction")
            finite(attack.hit_score, 0, 1)
            finite(attack.expected_damage, 0)
            if not isinstance(attack.reason, str) or not 1 <= len(attack.reason) <= 2000:
                raise ValueError("invalid attack reason")
            if attack.target_id is not None and attack.target_id not in {e.instance_id for e in snapshot.hostiles}:
                if not isinstance(attack.target_id, str) or not attack.target_id.startswith("terrain:"):
                    raise ValueError("unknown attack target")
            if attack.footprint is not None:
                if not isinstance(attack.footprint, tuple) or len(attack.footprint) != 5:
                    raise ValueError("invalid attack footprint")
                for number in attack.footprint[:4]:
                    finite(number, -HOST_LIMITS.max_coordinate_abs, HOST_LIMITS.max_coordinate_abs)
                finite(attack.footprint[4], 0, HOST_LIMITS.max_footprint_width)
            actual = attack.context or context
            if not isinstance(actual, ProposalContext): raise ValueError("invalid proposal context")
            for field in ("run_id", "room_visit_id", "build_revision", "catalog_revision"):
                if getattr(actual, field) != getattr(context, field): raise ValueError("stale proposal context")
            if (type(actual.observed_frame) is not int or type(actual.valid_until_frame) is not int or
                    actual.observed_frame != snapshot.frame or not snapshot.frame <= actual.valid_until_frame <= snapshot.frame+3):
                raise ValueError("stale or excessive proposal lease")
            if attack.wait is not None:
                wait = attack.wait
                if (not isinstance(wait, WaitEvidence) or not all(isinstance(x, str) and 0 < len(x) <= 2000 for x in (wait.token, wait.reason, wait.evidence)) or
                        type(wait.deadline_frame) is not int or not snapshot.frame < wait.deadline_frame <= snapshot.frame+max_wait_frames):
                    raise ValueError("expired or invalid wait evidence")
            result.append(replace(attack, context=actual))
        return result
    if method in ("route", "reposition"):
        if not isinstance(value, MotionProposal): raise ValueError("expected MotionProposal")
        return MotionProposal(path(value.waypoints), value.reason)
    if method == "firing_route": return list(path(value))
    if method in ("velocity", "launch_velocity"): return vector(value)
    if method == "features":
        if not isinstance(value, tuple) or len(value) != 3: raise ValueError("invalid features")
        for number in value: finite(number)
    elif method in ("hit_score",): finite(value, 0, 1)
    elif method == "pending_damage": finite(value, 0)
    elif method == "score": finite(value)
    elif method == "reject_goal":
        if type(value) is not bool: raise ValueError("expected recovery boolean")
    elif method == "begin_frame" and value is not None: raise ValueError("frame hook must not return actions")
    return value
