"""Mandatory progress contracts. Policy changes cannot reset their clocks."""
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class BoundaryEvent:
    kind: str
    frame: int
    age_ticks: int
    severity: str


@dataclass(frozen=True)
class ProgressEvidence:
    kind: str
    frame: int
    entity: str | None = None


class ProgressSupervisor:
    def __init__(self):
        self.previous = None
        self.last_progress = None
        self.last_damage = None
        self.idle_since = None
        self.blocked_since = None
        self.anchor = None
        self.route_best = {}
        self.route_anchors = {}
        self.history = deque(maxlen=151)
        self.events = []
        self.announced = {}
        self.recovery_level = 0
        self.recovery_count = 0
        self.max_no_progress = 0
        self.max_idle = 0
        self.boundary_counts = {}
        self.evidence = []
        self.danger_since = {}
        self.wait_until = None
        self.wait_consumed = False
        self.wait_credit = 0
        self.last_wait_frame = None
        self.recent_evidence = deque(maxlen=60)

    def observe(self, s, action=None, *, require_progress=False):
        self.events, self.evidence = [], []
        if s.paused or (self.previous and s.frame <= self.previous.frame): return None
        if self.last_progress is None: self.last_progress = self.last_damage = s.frame
        enemies = {e.instance_id: e for e in s.hostiles}
        if self.previous:
            old_ids = {e.instance_id for e in self.previous.hostiles}
            if any(d["instance_id"] in old_ids and d["frame"] >= self.previous.frame
                   for d in getattr(s, "recent_deaths", []) or []):
                self.evidence.append(ProgressEvidence("confirmed_npc_death", s.frame).__dict__)
                self.last_damage = s.frame
        if self.previous:
            for enemy in self.previous.hostiles:
                current = enemies.get(enemy.instance_id)
                # Disappearance is not proof of death (burrow/transform/partition).
                if current and current.hp < enemy.hp:
                    self.evidence.append(ProgressEvidence("enemy_hp_decreased", s.frame, enemy.instance_id).__dict__)
                    self.last_damage = s.frame
            if self.previous.layout.get("grid") != s.layout.get("grid"):
                old = self.previous.layout.get("grid") or {}
                new = s.layout.get("grid") or {}
                if any(g.get("type") == 14 and g.get("variant") == 0 and
                       g.get("collision", 0) != 0 and new.get(i, {}).get("collision", 0) == 0
                       for i, g in old.items()):
                    self.evidence.append(ProgressEvidence("breakable_obstacle_opened", s.frame).__dict__)
        if action:
            goal = action.diagnostics.get("route_goal")
            remaining = action.diagnostics.get("route_remaining")
            if goal is not None and remaining is not None:
                # Coarse endpoint identity tolerates a moving target; a new
                # endpoint or a stationary player's cheaper route earns no credit.
                key = (action.diagnostics.get("movement_target", action.target), round(goal[0]/80), round(goal[1]/80))
                if key not in self.route_best:
                    self.route_best[key] = remaining
                    self.route_anchors[key] = s.player.pos
                elif remaining <= self.route_best[key]-8 and s.player.pos.distance(self.route_anchors[key]) >= 8:
                    self.route_best[key] = remaining
                    self.route_anchors[key] = s.player.pos
                    self.evidence.append(ProgressEvidence("route_cost_decreased", s.frame, action.target).__dict__)
            idle = action.move == (0, 0) and action.shoot == (0, 0)
            self.idle_since = (self.idle_since if self.idle_since is not None else s.frame) if idle else None
            if action.move != (0, 0):
                if self.blocked_since is None:
                    self.blocked_since, self.anchor = s.frame, s.player.pos
                elif s.player.pos.distance(self.anchor) >= 8:
                    self.blocked_since, self.anchor = s.frame, s.player.pos
            else: self.blocked_since = None
        if self.evidence:
            self.recent_evidence.extend(self.evidence)
            self.last_progress = s.frame
            self.recovery_level = 0
            self.wait_consumed = False
            self.wait_credit = 0
            self.wait_until = None
        wait = action.diagnostics.get("wait") if action else None
        if wait and not self.wait_consumed and wait.get("evidence") and wait.get("reason") in ("weapon_charge", "cooldown", "known_invulnerability", "terrain_observation", "terrain_phase"):
            self.wait_until = min(s.frame+(600 if wait["reason"].startswith("terrain_") else 120 if wait["reason"] == "known_invulnerability" else 90), wait["deadline_frame"])
            self.wait_consumed = True
        waiting = self.wait_until is not None and s.frame < self.wait_until
        if waiting and self.previous:
            self.wait_credit += min(s.frame-self.previous.frame, max(0, self.wait_until-self.previous.frame))
        age = max(0, s.frame-self.last_progress-self.wait_credit)
        if waiting:
            self.idle_since = None
            self.blocked_since = None
        idle_age = s.frame-self.idle_since if self.idle_since is not None else 0
        blocked_age = s.frame-self.blocked_since if self.blocked_since is not None else 0
        self.max_no_progress = max(self.max_no_progress, age)
        self.max_idle = max(self.max_idle, idle_age)
        self.history.append((s.frame, round(s.player.pos.x/16), round(s.player.pos.y/16)))
        reasons = []
        if idle_age >= 30: reasons.append(("idle", idle_age))
        if blocked_age >= 30: reasons.append(("blocked", blocked_age))
        if age >= 60: reasons.append(("stall", age))
        if s.frame-self.last_damage >= 90 and age >= 90: reasons.append(("no_progress", age))
        if age >= 90 and len(self.history) >= 60:
            cells = [(x, y) for _, x, y in self.history]
            if len(set(cells)) <= 6: reasons.append(("oscillation", age))
        for kind, active in (("no_safe_candidate", action and action.diagnostics.get("no_safe_candidate")),
                             ("escape_margin", action and action.score.get("escape_slack", 100) < 6)):
            if active:
                self.danger_since.setdefault(kind, s.frame)
                duration = s.frame-self.danger_since[kind]
                if duration >= 15: reasons.append((kind, duration))
            else: self.danger_since.pop(kind, None)
        for kind, duration in reasons:
            if s.frame-self.announced.get(kind, -1000) >= 30:
                event = BoundaryEvent(kind, s.frame, duration, "critical" if kind in
                                      ("idle", "blocked", "no_safe_candidate", "escape_margin") else "warning")
                self.events.append(event)
                self.announced[kind] = s.frame
                self.boundary_counts[kind] = self.boundary_counts.get(kind, 0)+1
        new_level = 2 if age >= 120 else 1 if max(age, idle_age, blocked_age) >= 30 else 0
        if new_level > self.recovery_level:
            self.recovery_count += 1
        self.recovery_level = max(self.recovery_level, new_level)
        self.previous = s
        # Cleared rooms are handled by the independent clear-and-safety judge.
        if (require_progress or not s.room["is_clear"]) and age >= 240: return "STALL_UNRECOVERED"
        return None

    def metrics(self):
        return {"max_no_progress_ticks": self.max_no_progress, "max_idle_ticks": self.max_idle,
                "boundary_counts": self.boundary_counts, "recovery_entries": self.recovery_count}

    def feedback(self):
        return {**self.metrics(), "recent_evidence": list(self.recent_evidence),
                "trajectory_checkpoints": list(self.history)[::15], "recovery_level": self.recovery_level,
                "last_progress_frame": self.last_progress}
