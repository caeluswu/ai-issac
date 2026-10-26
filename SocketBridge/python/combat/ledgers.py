"""Health/resource facts, explicit cost intents and bounded revival waiting."""
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class HealthDelta:
    damage: int = 0
    bones_lost: int = 0
    voluntary_spend: int = 0
    container_loss: int = 0
    state: str = "alive"


@dataclass(frozen=True)
class HealthPoint:
    run_id: str
    frame: int
    hp: int
    dead: bool
    health: dict


class HealthLedger:
    def __init__(self):
        self.previous = None
        self.damage = self.bones_lost = self.voluntary_spend = self.container_loss = 0
        self.events = deque(maxlen=256)
        self.attempts = deque(maxlen=64)
        self.costs = {}
        self.revive_until = None
        self.state = "alive"
        self.last_key = None

    def damage_attempt(self, frame):
        self.attempts.append(frame)

    def authorize(self, intent_id, amount, expires_frame):
        if intent_id in self.costs or type(amount) is not int or amount <= 0 or len(self.costs) >= 16:
            raise ValueError("invalid/duplicate health cost")
        self.costs[intent_id] = (amount, expires_frame)

    def observe(self, s):
        key = (s.run_id, s.room_visit_id, s.frame, s.player.dead, tuple(sorted(s.health.items())))
        if key == self.last_key: return HealthDelta(state=self.state)
        if self.previous and self.previous.run_id != s.run_id: self.__init__()
        old = self.previous
        lost = max(0, old.hp-s.hp) if old else 0
        bones = max(0, old.health["bone_hearts"]-s.health["bone_hearts"]) if old else 0
        capacity = max(0, old.health.get("max_hearts", 0)-s.health.get("max_hearts", 0)) if old else 0
        attempted = any(old is not None and old.frame <= f <= s.frame for f in self.attempts)
        voluntary = 0
        for intent_id, (amount, expires) in list(self.costs.items()):
            if expires < s.frame: del self.costs[intent_id]
            elif lost and not attempted and lost <= amount:
                voluntary = lost; del self.costs[intent_id]; break
        container = min(lost-voluntary, capacity) if not attempted else 0
        damage = lost-voluntary-container
        self.damage += damage; self.bones_lost += bones
        self.voluntary_spend += voluntary; self.container_loss += container
        if s.player.dead:
            lives = max(s.health.get("extra_lives", 0), old.health.get("extra_lives", 0) if old else 0)
            if self.revive_until is None and lives > 0: self.revive_until = s.frame+120
            self.state = "reviving" if self.revive_until is not None and s.frame < self.revive_until else "dead"
        else:
            self.state = "alive"
            self.revive_until = None
        delta = HealthDelta(damage, bones, voluntary, container, self.state)
        if damage or bones or voluntary or container or (old and old.dead != s.player.dead):
            self.events.append({"snapshot_id": s.snapshot_id, **delta.__dict__})
        self.previous = HealthPoint(s.run_id, s.frame, s.hp, s.player.dead, dict(s.health))
        self.last_key = key
        return delta


class ResourceLedger:
    def __init__(self):
        self.previous = None
        self.events = deque(maxlen=256)
        self.spent = {"coins": 0, "bombs": 0, "keys": 0}

    def observe(self, s):
        current = {k: s.inventory.get(k) for k in self.spent}
        if self.previous and self.previous[0] == s.run_id:
            for key, value in current.items():
                old = self.previous[1][key]
                if value is not None and old is not None and value != old:
                    self.spent[key] += max(0, old-value)
                    self.events.append({"snapshot_id": s.snapshot_id, "resource": key, "delta": value-old})
        elif self.previous: self.__init__()
        self.previous = (s.run_id, current)
