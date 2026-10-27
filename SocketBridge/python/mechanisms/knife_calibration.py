"""Finite empty-room knife input/flight probe, not a combat policy."""
import math
import statistics
import time
from combat.boundary_safety import boundary_danger
from combat.geometry import GeometryService
from combat.models import Vec
from combat.item_capabilities import attack_identity
from combat.builds import digest


def charge_rate(calibration):
    """Observed release charge per confirmed held game update, if available."""
    samples=[]
    for cycle in calibration.get("cycles") or []:
        count=cycle.get("hold_updates")
        first=next((r["knife"].get("charge") for r in cycle.get("observations") or []
                    if r["knife"].get("is_flying") and type(r["knife"].get("charge")) in (int,float)),None)
        if type(count) is int and count>0 and type(first) in (int,float) and 0<first<=1.5:
            samples.append(first/count)
    return statistics.median(samples) if samples else None


async def probe_knife(session,start):
    if start.paused or start.hostiles or start.blocking_combat or boundary_danger(start):return None,start
    geometry=GeometryService(start)
    direction=next((d for d in ((1,0),(-1,0),(0,1),(0,-1)) if geometry.line_clear(start.player.pos,start.player.pos+Vec(x=d[0]*80,y=d[1]*80),shot=True)),None)
    if not direction:return None,start
    current=start;began=start.frame;wall=time.monotonic();rows=[];cycles=[]
    key=digest(["knife",attack_identity(start)])
    await session.release();await session.set_mode("FORCE_AI")
    async def advance(shoot):
        nonlocal current
        if (current.run_id,current.room_visit_id)!=(start.run_id,start.room_visit_id) or current.hp<start.hp or not current.alive or current.hostiles or boundary_danger(current):raise RuntimeError("KNIFE_PROBE_INTERRUPTED")
        if current.frame-began>=300 or time.monotonic()-wall>=12:raise TimeoutError("KNIFE_PROBE_BUDGET")
        if digest(["knife",attack_identity(current)])!=key:raise RuntimeError("KNIFE_PROBE_BUILD_CHANGED")
        if session.latest_acted==current.snapshot_id:current=await session.next_snapshot(after=current.snapshot_id)
        await session.send_action(current,(0,0),shoot,{"source":"knife_probe","scope_key":key})
        current=await session.next_snapshot(after=current.snapshot_id)
        for knife in current.projectiles.get("player_knives") or []:
            rows.append({"frame":current.frame,"input":list(shoot),"knife":dict(knife)})
    try:
        for _ in range(3):await advance((0,0))
        for duration in (3,10,20):
            hold_start=current.frame;held=set()
            while len(held)<duration:
                await advance(direction)
                for feedback in (current.weapon_feedback or {}).get("history") or []:
                    q,v=feedback.get("queried") or {},feedback.get("values") or {}
                    actual=(int(v.get("right",False))-int(v.get("left",False)),int(v.get("down",False))-int(v.get("up",False)))
                    if feedback.get("frame",-1)>=hold_start and feedback.get("consistent") and all(q.get(k) for k in ("left","right","up","down")) and actual==direction:
                        held.add(feedback["frame"])
                if current.frame-hold_start>duration+12:raise RuntimeError("KNIFE_INPUT_FEEDBACK_GAP")
            start_index=len(rows);await advance((0,0));flying=False;returned=False
            while current.frame-began<300:
                knives=current.projectiles.get("player_knives") or []
                flying=flying or any(k.get("is_flying") is True for k in knives)
                if flying and any(k.get("is_flying") is False for k in knives):returned=True;break
                if current.frame-hold_start>duration+90:break
                await advance((0,0))
            observed=rows[start_index:]
            distances=[r["knife"].get("distance") for r in observed if type(r["knife"].get("distance")) in (int,float)]
            speeds=[float(r["knife"]["knife_velocity"]) for r in observed
                    if r["knife"].get("is_flying") and type(r["knife"].get("knife_velocity")) in (int,float)]
            cycles.append({"hold_updates":len(held),"hold_started":hold_start,"end_frame":current.frame,
                "flying_observed":flying,"returned":returned,"max_distance":max(distances,default=0),
                "max_speed":max(speeds,default=0),"observations":observed})
            if not returned:break
            await advance((0,0))
        if not cycles:return None,current
        max_distance=max(c["max_distance"] for c in cycles);speeds=[c["max_speed"] for c in cycles if c["max_speed"]>0]
        value={"schema_version":1,"game_build":"1.7.9b.J835","item_id":114,"scope_key":key,
            "cycles":cycles,"max_observed_distance":max(40.,max_distance),"observed_speed":statistics.median(speeds) if speeds else 10.,
            "completed_cycles":sum(c["flying_observed"] and c["returned"] for c in cycles),
            "observability":{"knife_entities":bool(rows),"charge":any(type(r["knife"].get("charge")) in (int,float) for r in rows),
                "is_flying":any(type(r["knife"].get("is_flying")) is bool for r in rows)},
            "limitations":["finite_empty_room_probe_not_combat_hit_rate","raw_engine_charge_units_not_assumed"]}
        value["charge_per_held_update_estimate"]=charge_rate(value)
        value["calibration_id"]=digest(value)
        session.log("knife_probe_completed",calibration=value)
        return value,current
    finally:await session.release()
