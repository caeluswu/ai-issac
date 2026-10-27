"""Host prepares common geometry once; the model owns knife policy."""
from combat.models import Vec
from combat.motion import PlayerMotion

DIRECTIONS=((1,0),(-1,0),(0,1),(0,-1))


def confirmed_held_updates(s):
    """Consecutive engine-queried nonzero fire updates in the retained window."""
    by_frame={}
    for row in (s.weapon_feedback or {}).get("history") or []:
        frame=row.get("frame")
        if type(frame) is not int or not row.get("consistent"):continue
        if not all((row.get("queried") or {}).get(k) for k in ("left","right","up","down")):continue
        values=row.get("values") or {}
        by_frame[frame]=any(values.get(k) for k in ("left","right","up","down"))
    if not by_frame:return 0
    latest=max(by_frame)
    if s.frame-latest>2:return 0
    count=0
    while by_frame.get(latest-count) is True:count+=1
    return count


def prepare_batch(runtime,s,build,profile,geometry,moves,weapon,aim):
    candidates=[]
    for index,move in enumerate(moves,1):
        _,velocity,_,_=PlayerMotion.step(s.player.pos,s.player.vel,PlayerMotion.desired(move,build),geometry,build)
        shots=[]
        for direction in DIRECTIONS:
            launch=weapon.launch_velocity(s,build,aim,direction,velocity)
            shots.append({"shoot":list(direction),"velocity":launch.model_dump()})
        candidates.append({"id":index,"move":list(move),"velocity":velocity.model_dump(),"shots":shots})
    enemies=[{"id":e.instance_id,"pos":e.pos.model_dump(),"vel":e.vel.model_dump(),"radius":e.collision_radius,
        "hp":e.hp,"vulnerable":e.vulnerable,"dead":e.dead,"visible":geometry.line_clear(s.player.pos,e.pos,shot=True)} for e in s.hostiles[:64]]
    damage=weapon.damage_for_hold() if hasattr(weapon,"damage_for_hold") else build.damage_per_hit
    calibration=getattr(runtime,"calibration_summary",None)
    knives=[]
    for observed in s.projectiles.get("player_knives") or []:
        knife=dict(observed)
        knife["engine_charge"]=knife.get("charge")
        held=confirmed_held_updates(s) if knife.get("is_flying") is False else 0
        knife["held_updates_observed"]=held
        rate=(calibration or {}).get("charge_per_held_update_estimate")
        if held and type(rate) in (int,float) and rate>0:
            knife["charge"]=min(1.,held*rate)
            knife["charge_source"]="engine_feedback_estimate"
        else:knife["charge_source"]="engine"
        knives.append(knife)
    context={"run_id":s.run_id,"room_visit_id":s.room_visit_id,"snapshot_id":s.snapshot_id,"frame":s.frame,
        "build_signature":build.signature,"player":{"pos":s.player.pos.model_dump(),"vel":s.player.vel.model_dump(),"radius":s.player.collision_radius},
        "damage_per_hit":damage or build.damage_per_hit,"range":build.effective_range,"tear_radius":5,
        "fire_interval":build.fire_interval,"enemies":enemies,"family":build.family,
        "knives":knives,
        "calibration":calibration,
        "weapon_feedback":(s.weapon_feedback or {}).get("history") or [],
        "weapon_feedback_detail":s.weapon_feedback,
        "projectiles":[{"id":t.get("instance_id",str(t.get("id"))),"pos":t["pos"],"vel":t["vel"],
            "radius":t.get("collision_radius",5),"damage":t.get("collision_damage",build.damage_per_hit)}
            for t in (s.projectiles.get("player_tears") or [])[:64] if t.get("owner_type",1)==1]}
    value=runtime.evaluate(context,candidates,feedback={"applied_action_id":s.applied_action_id})
    weapon.attack_model_results=value;weapon.attack_model_damage=context["damage_per_hit"]
    return value
