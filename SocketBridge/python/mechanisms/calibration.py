"""Scoped, finite charge probes. Measurements are separate from model source."""
from copy import deepcopy
import math
import statistics
import time
from combat.builds import digest
from combat.item_capabilities import attack_identity
from combat.boundary_safety import boundary_danger
from combat.geometry import GeometryService
from combat.models import Vec
from .program import ProgramError


def calibration_key(s):return digest(["charge-j835-v2",attack_identity(s)])


def validate_runtime_calibration(value):
    if value.get("schema_version")==1:
        from .validation import validate_calibration
        validate_calibration(value);return
    if value.get("schema_version")!=2 or value.get("game_build")!="1.7.9b.J835" or value.get("item_id")!=69:
        raise ProgramError("INVALID_RUNTIME_CALIBRATION")
    if value.get("source") not in ("estimated","observed") or not isinstance(value.get("scope_key"),str):raise ProgramError("CALIBRATION_SCOPE")
    low,high=value.get("min_charge_frames"),value.get("max_charge_frames")
    if type(low) is not int or type(high) is not int or not 1<=low<=high<=90:raise ProgramError("CALIBRATION_TIME_RANGE")
    speed=value.get("tear_speed")
    if type(speed) not in (int,float) or not math.isfinite(speed) or not 0<speed<1000:raise ProgramError("CALIBRATION_SPEED")
    samples=value.get("damage_samples")
    if not isinstance(samples,list) or not 1<=len(samples)<=16:raise ProgramError("CALIBRATION_SAMPLES")
    for row in samples:
        if not isinstance(row,list) or len(row)!=2 or type(row[0]) is not int or not 1<=row[0]<=90 or type(row[1]) not in (int,float) or not math.isfinite(row[1]) or not 0<=row[1]<=1000000:
            raise ProgramError("CALIBRATION_SAMPLE_VALUE")
    if value["source"]=="observed" and not value.get("observations"):raise ProgramError("CALIBRATION_REQUIRES_OBSERVATIONS")


def estimated_calibration(s,baseline):
    ratio=float(s.stats.get("damage",3.5))/baseline["scope"]["damage"]
    value={"schema_version":2,"item_id":69,"game_build":"1.7.9b.J835","source":"estimated",
        "scope_key":calibration_key(s),"attack_identity":attack_identity(s),
        "min_charge_frames":3,"max_charge_frames":35,"tear_speed":10*float(s.stats.get("shot_speed",1)),
        "damage_samples":[[n,d*ratio] for n,d in baseline["damage_samples"] if n<=35],
        "observations":[],"limitations":["scaled_base_prior_not_measured_for_this_build","finite_probe_not_exact_formula"]}
    value["calibration_id"]=digest(value);validate_runtime_calibration(value);return value


async def probe_charge(session,start,baseline):
    """At most two passes/300 updates/12 wall seconds, no console operations."""
    if start.hostiles or start.blocking_combat or boundary_danger(start) or start.paused:return None,start
    geometry=GeometryService(start)
    direction=next((d for d in ((1,0),(-1,0),(0,1),(0,-1)) if geometry.line_clear(start.player.pos,start.player.pos+Vec(x=d[0]*80,y=d[1]*80),shot=True)),None)
    if direction is None:return None,start
    key=calibration_key(start);began=start.frame;wall=time.monotonic();current=start;samples=[];seen=set()
    await session.release();await session.set_mode("FORCE_AI")
    async def advance(shoot):
        nonlocal current
        if (current.run_id,current.room_visit_id)!=(start.run_id,start.room_visit_id) or not current.alive or current.hp<start.hp or current.hostiles or current.blocking_combat or boundary_danger(current):
            raise RuntimeError("CHARGE_PROBE_INTERRUPTED")
        if current.frame-began>=300 or time.monotonic()-wall>=12:raise TimeoutError("CHARGE_PROBE_BUDGET")
        if calibration_key(current)!=key:raise RuntimeError("CHARGE_PROBE_BUILD_CHANGED")
        if session.latest_acted==current.snapshot_id:current=await session.next_snapshot(after=current.snapshot_id)
        await session.send_action(current,(0,0),shoot,{"source":"charge_probe","scope_key":key})
        current=await session.next_snapshot(after=current.snapshot_id)
    try:
        for _ in range(3):await advance((0,0))
        for round_number in range(2):
            for desired in (3,10,20,35):
                ids={t.get("instance_id") for t in current.projectiles.get("player_tears") or []}
                held_frames=set();hold_start=current.frame
                while len(held_frames)<desired:
                    await advance(direction)
                    for row in (current.weapon_feedback or {}).get("history",[]):
                        q,v=row.get("queried") or {},row.get("values") or {}
                        if row.get("frame",-1)<hold_start or row.get("consistent") is not True or not all(q.get(k) for k in ("left","right","up","down")):continue
                        actual=(int(v.get("right",False))-int(v.get("left",False)),int(v.get("down",False))-int(v.get("up",False)))
                        if actual==direction:held_frames.add(row["frame"])
                    if current.frame-hold_start>desired+12:raise RuntimeError("CHARGE_PROBE_FEEDBACK_GAP")
                await advance((0,0))
                found=False
                for _ in range(4):
                    for tear in current.projectiles.get("player_tears") or []:
                        identity=tear.get("instance_id");damage=tear.get("collision_damage")
                        if tear.get("owner_type")==1 and identity and identity not in ids and identity not in seen and type(damage) in (int,float) and math.isfinite(damage):
                            speed=Vec(**tear["vel"]).length()
                            if 0<speed<1000:
                                samples.append({"held_frames":len(held_frames),"damage":damage,"speed":speed,"tear_id":identity,
                                    "frame":current.frame,"run_id":current.run_id,"room_visit_id":current.room_visit_id,"round":round_number})
                                seen.add(identity);found=True
                    if found:break
                    await advance((0,0))
                await advance((0,0))
            if len(samples)>=3:break
        if not samples:return None,current
        result=estimated_calibration(start,baseline)
        result.update(source="observed",observations=samples,
            damage_samples=sorted([[s["held_frames"],s["damage"]] for s in samples]),
            tear_speed=statistics.median(s["speed"] for s in samples),
            min_charge_frames=min(s["held_frames"] for s in samples),
            max_charge_frames=min(35,max(s["held_frames"] for s in samples)))
        result["calibration_id"]=digest({k:v for k,v in result.items() if k!="calibration_id"})
        validate_runtime_calibration(result)
        session.log("charge_probe_completed",scope_key=key,calibration=result)
        return result,current
    finally:await session.release()
