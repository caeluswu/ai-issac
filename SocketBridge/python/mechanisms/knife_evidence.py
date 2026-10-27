"""Independent, conservative proof of knife use from a saved game trace."""
from collections import defaultdict
import json
from pathlib import Path
import math


def summarize(trace, source_sha256, report, probe=None):
    """A callback is an attempt; only later observed HP loss/death confirms it."""
    frames=defaultdict(dict);attempts=[];actions=[];model_actions=set();failures=[]
    with Path(trace).open(encoding="utf-8") as stream:
        for line in stream:
            row=json.loads(line)
            if row.get("kind")=="game_message":
                message=row.get("message") or {}
                state=(message.get("payload") or {}).get("COMBAT_FRAME")
                if state:
                    key=(state.get("run_id"),state.get("room_visit_id"))
                    frames[key][state["frame"]]=state
                if message.get("event")=="KNIFE_DAMAGE_ATTEMPT":attempts.append(message.get("data") or {})
            elif row.get("kind")=="action_sent":
                actions.append(row)
                if str(((row.get("metadata") or {}).get("diagnostics") or {}).get("fire_reason","")).startswith("model_knife_"):
                    model_actions.add(row.get("action_id"))
            elif row.get("kind")=="component_fallback":failures.append(row)
    releases=[];returns=[];midrange=[];thrown_hits=[];ambiguous=[];observed=set();launch_samples=[]
    actions_by_frame={(a.get("room_visit_id"),a.get("frame")):a for a in actions}
    for key,room in frames.items():
        previous={};active={};starts={}
        for frame,state in sorted(room.items()):
            player=(state.get("player") or {}).get("pos") or {}
            enemies={e.get("instance_id"):e for e in state.get("enemies") or []}
            for knife in ((state.get("projectiles") or {}).get("player_knives") or []):
                identity=knife.get("instance_id")
                if identity is None:continue
                observed.add(identity)
                flight=knife.get("is_flying")
                if flight is True and previous.get(identity) is False:
                    separations={}
                    for eid,e in enemies.items():
                        p=e.get("pos") or {}
                        if not all(type(v) in (int,float) for v in (p.get("x"),p.get("y"),player.get("x"),player.get("y"))):continue
                        d=((p["x"]-player["x"])**2+(p["y"]-player["y"])**2)**.5
                        separations[eid]=d-(e.get("collision_radius") or 0)-(state.get("player") or {}).get("collision_radius",0)
                    starts[identity]=dict(frame=frame,enemy_separations=separations)
                    active[identity]=frame;releases.append(dict(room=key,knife=identity,frame=frame))
                    near=min((e for e in enemies.values() if e.get("pos") and e.get("vulnerable",True)),
                             key=lambda e:math.dist((player.get("x",0),player.get("y",0)),
                                                    (e["pos"].get("x",0),e["pos"].get("y",0))),default=None)
                    recent=next((actions_by_frame[(key[1],f)] for f in range(frame-1,max(-1,frame-5),-1)
                                 if (key[1],f) in actions_by_frame and actions_by_frame[(key[1],f)].get("shoot") not in ([0,0],(0,0))),None)
                    if near:
                        dx=near["pos"]["x"]-player.get("x",0);dy=near["pos"]["y"]-player.get("y",0)
                        direction=(recent or {}).get("shoot") or [0,0]
                        lateral=abs(dx) if direction[1] else abs(dy) if direction[0] else None
                        launch_samples.append({"frame":frame,"target_id":near.get("instance_id"),
                            "relative_x":round(dx,1),"relative_y":round(dy,1),"held_direction":direction,
                            "lateral_offset_px":round(lateral,1) if lateral is not None else None,
                            "target_collision_radius":near.get("collision_radius"),"charge_at_release":knife.get("charge")})
                if flight is False and previous.get(identity) is True and identity in active:
                    returns.append(dict(room=key,knife=identity,frame=frame,launch_frame=active.pop(identity)))
                if flight is not None:previous[identity]=flight
                if flight is True and identity in active:
                    # The knife is still flying at this snapshot. A later HP
                    # observation is needed; the game callback can be rejected.
                    for event in attempts:
                        if event.get("run_id")!=key[0] or event.get("room_visit_id")!=key[1]:continue
                        if event.get("source_knife_id")!=identity or event.get("callback_frame")!=frame:continue
                        if starts.get(identity,{}).get("enemy_separations",{}).get(event.get("target_id"),-1)>8:
                            midrange.append(event)
    unique_attempts={(e.get("run_id"),e.get("room_visit_id"),e.get("callback_frame"),e.get("source_knife_id"),e.get("target_id")):e for e in attempts}
    def confirmed(event):
        key=(event.get("run_id"),event.get("room_visit_id"));room=frames.get(key,{})
        tick=event.get("callback_frame");target=event.get("target_id")
        if type(tick) is not int:return None
        before=[(f,e) for f,s in room.items() if tick-3<=f<=tick for e in s.get("enemies") or [] if e.get("instance_id")==target]
        after=[(f,e) for f,s in room.items() if tick<=f<=tick+3 for e in s.get("enemies") or [] if e.get("instance_id")==target]
        if before and after and min(e.get("hp",float("inf")) for _,e in after)<max(e.get("hp",float("-inf")) for _,e in before):
            return dict(target=target,callback_frame=tick,source_knife_id=event.get("source_knife_id"),proof="observed_hp_drop")
        if before and any(any(d.get("instance_id")==target for d in s.get("recent_deaths") or [])
                          for f,s in room.items() if tick<=f<=tick+3):
            return dict(target=target,callback_frame=tick,source_knife_id=event.get("source_knife_id"),proof="observed_death")
        return None
    confirmed_all=[value for e in unique_attempts.values() if (value:=confirmed(e))]
    midrange_keys={(e.get("run_id"),e.get("room_visit_id"),e.get("callback_frame"),e.get("source_knife_id"),e.get("target_id")) for e in midrange}
    for key in midrange_keys:
        event=unique_attempts[key];value=confirmed(event)
        if value:thrown_hits.append(value)
        else:ambiguous.append(event)
    return dict(schema_version=1,result=report.result,source_sha256=source_sha256,
                completed_probe_cycles=(probe or {}).get("completed_cycles",0),
                observed_knife_entities=len(observed),observed_flight_starts=len(releases),observed_returns=len(returns),
                launch_samples=launch_samples[:12],
                confirmed_knife_hits_total=len(confirmed_all),
                midrange_knife_damage_attempts=len(midrange),confirmed_thrown_hits=len(thrown_hits),
                thrown_hit_samples=thrown_hits[:24],ambiguous_attempts=len(ambiguous),model_actions=len(model_actions),
                accepted_actions=report.accepted_actions,damage_half_hearts=report.damage_half_hearts,
                combat_ticks=report.combat_ticks,wall_seconds=report.wall_seconds,
                component_failures=failures[:16],
                passed=bool(report.result=="CLEARED" and (probe or {}).get("completed_cycles",0)>=3 and
                            len(thrown_hits)>0 and model_actions and not failures))
