"""Observed build transactions; item descriptions never add to panel stats."""
from collections import deque
import hashlib
import json
import math
import statistics
from .models import BuildProfile, Vec


# Narrow host support, verified against the pinned EID package by P5 tooling.
# These are implementation support rules, not estimates of item strength.
ITEM_SUPPORT = {105: "inactive_active", 1: "attributes", 4: "attributes", 12: "attributes", 20: "flight"}


def collectibles(s):
    items = s.inventory.get("collectibles") or {}
    # V1 scanned the undefined 733 slot in the pinned vanilla build and read
    # pointer-like counts. Keep raw evidence, exclude it from semantic state.
    # V2 uses runtime catalog coverage and must never apply this migration.
    return {k: v for k, v in items.items() if s.schema_version != 1 or int(k) <= 732}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def observed_build(s):
    effects = s.effects.model_dump() if s.effects else None
    if effects:
        # Cooldown changes every frame; effect identity/count changes semantics.
        effects = {"status": effects["status"], "items": sorted(
            [(e["kind"], e["item_id"], e["count"]) for e in effects["items"]], key=str)}
    active = {k: v.get("item") for k, v in (s.inventory.get("active_items") or {}).items()}
    return {
        "inventory": {"collectibles": collectibles(s),
                      "trinkets": [s.inventory.get("trinket_0", 0), s.inventory.get("trinket_1", 0)], "active": active},
        "attack": {k: s.stats.get(k) for k in ("weapon_type", "damage", "max_fire_delay", "tear_range", "shot_speed", "tear_height", "tear_falling_speed")},
        "effects": effects,
        "movement": {"speed": s.stats["speed"], "fly": s.stats["can_fly"], "radius": s.player.collision_radius},
        "health": {"type": s.stats["player_type"], "max_hearts": s.health.get("max_hearts"),
                   "bone_hearts": s.health["bone_hearts"], "extra_lives": s.health.get("extra_lives")},
    }


class NormalTearsBuild:
    def __init__(self):
        self.profile = BuildProfile()
        self.seen = set()
        self.seen_order = deque()
        self.speeds = deque(maxlen=60)
        self.revision = 0
        self.previous = None
        self.run_id = None
        self.calibration_scope = None

    def update(self, s):
        if s.stats["weapon_type"] != 1 or s.stats["player_type"] != 0:
            raise ValueError("UNSUPPORTED_BUILD: no compatible observed weapon/character")
        unknown = [int(k) for k, v in collectibles(s).items() if v and int(k) not in ITEM_SUPPORT]
        trinkets = [s.inventory.get(k, 0) for k in ("trinket_0", "trinket_1")]
        effects = s.effects.items if s.effects else []
        if unknown or any(trinkets) or any(e.count and (e.kind != "collectible" or e.item_id not in ITEM_SUPPORT) for e in effects):
            raise ValueError("UNSUPPORTED_BUILD: unmapped item/effect capabilities")
        if any(v.get("item") not in ITEM_SUPPORT for v in (s.inventory.get("active_items") or {}).values()):
            raise ValueError("UNSUPPORTED_BUILD: active inventory capability unknown")
        if s.inventory_coverage and s.inventory_coverage.static_status != "known":
            raise ValueError("UNSUPPORTED_BUILD: incomplete inventory observation")
        if s.effects and s.effects.status != "known": raise ValueError("UNSUPPORTED_BUILD: temporary effects unknown")
        state = observed_build(s)
        if self.run_id != s.run_id:
            self.revision, self.previous, self.run_id = 0, None, s.run_id
        changes = [k for k in state if self.previous is None or state[k] != self.previous[k]]
        if changes: self.revision += 1
        # Old in-flight tears must not calibrate a newly changed attack model.
        calibration = digest({k: state[k] for k in ("attack", "inventory", "effects")})
        scope = (s.run_id, calibration)
        if self.calibration_scope != scope:
            self.speeds.clear(); self.seen.clear(); self.seen_order.clear()
            for tear in s.projectiles["player_tears"] or []:
                self.seen.add((s.room_visit_id, tear.get("instance_id", tear.get("id"))))
            self.calibration_scope = scope
        p = self.profile.model_copy(deep=True)
        p.revision = self.revision
        p.signature = digest(state)
        p.calibration_key = calibration
        p.changed_domains = changes
        p.damage_per_hit = float(s.stats.get("damage", 3.5))
        p.player_displacement_scale = 2.
        p.move_speed = max(1, float(s.stats["speed"])*4.4)*p.player_displacement_scale
        p.effective_range = max(40, float(s.stats["tear_range"])*.75)
        p.fire_interval = max(1, float(s.stats.get("max_fire_delay", 10))+1)
        p.inheritance_available = bool(s.movement_inheritance)
        for tear in s.projectiles["player_tears"] or []:
            key = (s.room_visit_id, tear.get("instance_id", tear.get("id")))
            if key in self.seen: continue
            self.seen.add(key); self.seen_order.append(key)
            while len(self.seen_order) > 2048: self.seen.discard(self.seen_order.popleft())
            owner = tear.get("owner_type")
            if owner is not None and owner != 1: continue
            if s.player.pos.distance(Vec(**tear["pos"])) < 35 and s.player.vel.length() < .5:
                speed = Vec(**tear["vel"]).length()
                if math.isfinite(speed) and 0 < speed < 1000: self.speeds.append(speed)
        p.calibration_samples = len(self.speeds)
        p.calibrated = len(self.speeds) >= 3
        p.tear_speed = statistics.median(self.speeds) if self.speeds else 10*float(s.stats["shot_speed"])
        self.previous = state
        # Revalidate values read through flexible legacy dictionaries.
        self.profile = BuildProfile.model_validate(p.model_dump())
        return self.profile.model_copy(deep=True)


class BuildResolver:
    def __init__(self, adapters=None):
        self.adapters = {1: NormalTearsBuild()} if adapters is None else adapters
        self.weapon_type = None
        self.epoch = 0

    def update(self, s):
        weapon = s.stats["weapon_type"]
        if weapon not in self.adapters: raise ValueError("UNSUPPORTED_BUILD: unregistered weapon adapter")
        if self.weapon_type is not None and weapon != self.weapon_type:
            raise ValueError("UNSUPPORTED_BUILD: weapon transition needs a compatible published transaction")
        self.weapon_type = weapon
        return self.adapters[weapon].update(s)
