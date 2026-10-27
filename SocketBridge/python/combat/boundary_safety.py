"""Clear-room eligibility; permanent distant terrain is handled by path safety."""
from .models import Vec


def boundary_danger(s):
    if (s.bombs or s.projectiles.get("enemy_projectiles")
            or any(x.get("is_enemy",True) for x in s.projectiles.get("lasers") or [])
            or getattr(s,"environment_residuals",[])):
        return True
    for enemy in s.hostiles:
        if enemy.can_shut_doors: return True
        if enemy.collision_class != 0 and s.player.pos.distance(enemy.pos) <= s.player.collision_radius+enemy.collision_radius+8:
            return True
    for entity in s.enemies:
        if (entity.type == 291 and entity.variant in (0,1,2) and not entity.dead
                and entity.collision_class != 0
                and s.player.pos.distance(entity.pos) <= s.player.collision_radius+entity.collision_radius+8):
            return True
    for hazard in s.hazards:
        if hazard.get("is_extinguished") is True: continue
        static = (hazard.get("type") == "FIREPLACE" and hazard.get("is_shooting") is False) or (
            hazard.get("type") == "EFFECT" and hazard.get("mechanism") in ("creep_or_slowing_surface","fire"))
        if not static or "pos" not in hazard or "collision_radius" not in hazard: return True
        if s.player.pos.distance(Vec(**hazard["pos"])) <= s.player.collision_radius+hazard["collision_radius"]+8:
            return True
    return False
