"""Training-only J835 Mom entity/phase probe; never counts as M3 completion."""
import argparse
import asyncio
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"SocketBridge/python"))
from facade import SocketBridge
from combat.launcher import GameLauncher
from combat.runtime import CombatSession,NativePause,RunLog


async def run(updates,seed):
    directory=ROOT/"artifacts/runs"/datetime.now(timezone.utc).strftime("mom-probe-%Y%m%dT%H%M%S%fZ")
    log=RunLog(directory);log.manifest(ROOT,None)
    pause=NativePause(game_executable=ROOT/"The Binding of Isaac Rebirth Repentance/isaac-ng.exe")
    launcher=GameLauncher(ROOT,log,pause,slot=1,timeout=75)
    session=CombatSession(SocketBridge(),None,pause,log,ready_timeout=75,new_run=True,seed=seed)
    print("Mom probe artifacts: "+str(directory),flush=True)
    report={"source_mode":"diagnostic_initialization","formal_run_proof":False,"seed":seed,
            "commands":[],"observations":[],"frames":[],"result":"INCOMPLETE"}
    try:
        s=await session.open(bootstrap=launcher)
        if s.floor.stage!=1 or s.blocking_combat:raise RuntimeError("MOM_PROBE_START_NOT_CLEAR")
        for command,target in (("stage 6",lambda x:x.floor and x.floor.stage==6 and x.room.get("is_starting_room")),
                               ("goto s.boss.1060",lambda x:x.floor and x.floor.stage==6 and x.room.get("room_type")==5)):
            log("initialization_command",command=command,source_mode="diagnostic_initialization")
            await session.rpc("EXEC_CONSOLE",{"command":command})
            report["commands"].append(command)
            deadline=time.monotonic()+20
            while not target(s):
                if time.monotonic()>deadline:raise TimeoutError("MOM_PROBE_TRANSITION_TIMEOUT: "+command)
                s=await session.next_snapshot(after=s.snapshot_id,timeout=15)
        began=s.frame;seen=set()
        while s.frame-began<updates and s.alive and s.floor.stage==6 and s.room.get("room_type")==5:
            report["frames"].append({"frame":s.frame,"player_pos":s.player.pos.model_dump(),
                                     "health":s.health,"enemies":[{"id":e.instance_id,"type":e.type,
                                     "variant":e.variant,"state":e.state,"state_frame":e.state_frame,
                                     "pos":e.pos.model_dump(),"vel":e.vel.model_dump(),
                                     "collision_class":e.collision_class,"contact_damage":e.contact_damage,
                                     "vulnerable":e.vulnerable,"hp":e.hp} for e in s.enemies
                                     if e.type==45 or e.is_boss],"hazards":s.hazards})
            enemies=[{"id":e.instance_id,"type":e.type,"variant":e.variant,"subtype":e.subtype,
                      "is_boss":e.is_boss,"state":e.state,"state_frame":e.state_frame,
                      "hp":e.hp,"pos":e.pos.model_dump(),"collision_radius":e.collision_radius}
                     for e in s.enemies if e.type==45 or e.is_boss]
            for e in enemies:
                key=(e["id"],e["state"])
                if key not in seen:
                    report["observations"].append({"frame":s.frame,"room_visit_id":s.room_visit_id,
                                                    "room_clear":s.room.get("is_clear"),
                                                    "player_pos":s.player.pos.model_dump(),
                                                    "player_health":s.health,
                                                    "hazards":s.hazards,"entity":e})
                    seen.add(key)
            s=await session.next_snapshot(after=s.snapshot_id,timeout=8)
        report.update(result="OBSERVED" if any(row["entity"]["type"] == 45 for row in report["observations"])
                      else "NO_MOM_ENTITY_OBSERVED",
                      final_frame=s.frame,final_stage=s.floor.stage if s.floor else None,
                      final_room_type=s.room.get("room_type"),alive=s.alive,
                      entity_states=len(seen))
        return 0 if report["result"]=="OBSERVED" else 1
    except Exception as exc:
        report.update(result="FAILED",error=type(exc).__name__+": "+str(exc))
        return 1
    finally:
        try:await session.close()
        finally:
            launcher.collect_evidence()
            (directory/"mom-probe.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
            log.file.close()
            print(json.dumps({k:v for k,v in report.items() if k not in ("observations","frames")},ensure_ascii=False),flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--updates",type=int,default=120)
    parser.add_argument("--seed",default="3BYH A82R")
    args=parser.parse_args()
    if not 1<=args.updates<=600:parser.error("updates must be 1..600")
    raise SystemExit(asyncio.run(run(args.updates,args.seed)))
