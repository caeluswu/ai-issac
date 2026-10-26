from combat.models import WorldSnapshot, ContextRef


def snapshot(frame=10, enemies=True, **updates):
    width, height = 15, 9
    cells = {str(y*width+x): {"x": x*40, "y": y*40, "inside": 0 < x < width-1 and 0 < y < height-1,
                              "collision": 0 if 0 < x < width-1 and 0 < y < height-1 else 4}
             for y in range(height) for x in range(width)}
    data = {"schema_version": 1, "run_id": "test", "room_visit_id": "test:1", "frame": frame,
            "paused": False, "mode": "FORCE_AI", "seed": "TEST SEED",
            "room": {"room_idx": 84, "stage": 1, "difficulty": 0, "is_clear": not enemies,
                     "enemy_count": int(enemies), "is_starting_room": not enemies},
            "layout": {"cells": cells, "width": width, "grid": {}, "doors": {
                "2": {"target_room": 85, "target_room_type": 1, "is_open": True, "is_locked": False, "x": 560, "y": 160}}},
            "player": {"pos": {"x": 280, "y": 160}, "vel": {"x": 0, "y": 0}, "collision_radius": 10, "dead": False},
            "stats": {"player_type": 0, "weapon_type": 1, "speed": 1, "tear_range": 300, "shot_speed": 1, "can_fly": False},
            "health": {"red_hearts": 6, "soul_hearts": 0, "bone_hearts": 0},
            "inventory": {"collectibles": {"105": 1}}, "movement_inheritance": {},
            "enemies": [{"instance_id": "e1", "type": 10, "variant": 0, "pos": {"x": 400, "y": 160},
                         "vel": {"x": 0, "y": 0}, "hp": 10, "max_hp": 10, "collision_radius": 10,
                         "vulnerable": True, "friendly": False, "dead": False, "collision_class": 4}] if enemies else [],
            "projectiles": {"enemy_projectiles": [], "player_tears": [], "lasers": [], "friendly_tears": []},
            "bombs": [], "hazards": []}
    data.update(updates)
    return WorldSnapshot.model_validate(data)


def context(s):
    return ContextRef(run_id=s.run_id, room_visit_id=s.room_visit_id, snapshot_id=s.snapshot_id,
                      floor_instance_id=s.floor.instance_id if s.floor else None)
