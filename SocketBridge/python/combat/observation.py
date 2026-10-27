"""Atomic snapshots and an outcome judge independent of policy decisions."""
import time
from .models import WorldSnapshot
from .ledgers import HealthLedger


class SnapshotAssembler:
    def __init__(self):
        self.latest = None
        self.received_at = 0.
        self.generation = 0
        self.error = None

    def consume(self, message):
        value = (message.get("payload") or {}).get("COMBAT_FRAME")
        if value is None: return None
        try: s = WorldSnapshot.model_validate(value)
        except Exception as exc:
            self.error = str(exc); return None
        if self.latest and s.run_id == self.latest.run_id:
            if s.frame < self.latest.frame: return None
            if s.snapshot_id == self.latest.snapshot_id and s.paused == self.latest.paused: return None
        residual = []
        if self.latest and s.room_visit_id == self.latest.room_visit_id:
            residual = [h for h in getattr(self.latest, "environment_residuals", []) if h["expires_frame"] >= s.frame]
            previous_grid = self.latest.layout["grid"] or {}
            for index, grid in (s.layout["grid"] or {}).items():
                old = previous_grid.get(index)
                if grid["type"] == 12 and old and old["state"] != grid["state"] and grid["state"] == 4:
                    residual.append({"id": index, "x": grid["x"], "y": grid["y"], "expires_frame": s.frame+6})
        s.environment_residuals = residual
        self.latest, self.received_at = s, time.monotonic()
        self.generation += 1; self.error = None
        return s

    def fresh(self):
        return self.latest is not None and time.monotonic()-self.received_at <= 2/30+.025


class CombatJudge:
    def __init__(self, timeout_ticks=120*30):
        self.timeout_ticks = timeout_ticks
        self.previous = None
        self.damage = 0
        self.bones_lost = 0
        self.clear_frames = 0
        self.started = None
        self.last_progress = None
        self.enemy_hp = {}
        self.events = []
        self.health = HealthLedger()

    def observe(self, s):
        self.events = []
        if self.started is None: self.started = self.last_progress = s.frame
        if self.previous and self.health.previous is None: self.health.observe(self.previous)
        delta = self.health.observe(s)
        self.damage, self.bones_lost = self.health.damage, self.health.bones_lost
        if delta.damage or delta.bones_lost: self.events.append("confirmed_damage")
        current = {e.instance_id: e.hp for e in s.hostiles}
        if any(i in current and current[i] < hp for i, hp in self.enemy_hp.items()): self.last_progress = s.frame
        if set(current)-set(self.enemy_hp) and self.previous: self.events.append("new_enemy_mechanism")
        self.enemy_hp = current
        self.previous = s
        if delta.state == "dead": return "DEAD"
        if delta.state == "reviving": return None
        if s.room["is_clear"] and s.room["enemy_count"] == 0 and not any(e.can_shut_doors for e in s.hostiles):
            self.clear_frames += 1
        else: self.clear_frames = 0
        if self.clear_frames >= 3: return "CLEARED"
        if s.frame-self.started >= self.timeout_ticks: return "COMBAT_TIMEOUT"
        # Without validated mechanism knowledge, invulnerability is unknown,
        # not an invented wait stage. Replanning still obeys the debounce.
        if s.frame-self.last_progress >= 90: self.events.append("no_progress")
        return None
