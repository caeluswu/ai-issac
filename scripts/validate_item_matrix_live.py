"""One J835 first-room trial for ordinary attributes or 69 plus attributes.

Development setup grants only the requested collectibles before measurement.
No DeepSeek request, console combat command or artificial combat damage.
"""
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
from mechanisms.weapon import AdaptiveChargedBuild
from validate_mechanism_live import proof_from_trace


async def run(args):
    name='charge' if args.charge else 'attributes'
    directory=ROOT/"artifacts/runs"/datetime.now(timezone.utc).strftime(f"m2c-{name}-%Y%m%dT%H%M%S%fZ")
    log=RunLog(directory);log.manifest(ROOT,None)
    pause=NativePause(game_executable=ROOT/"The Binding of Isaac Rebirth Repentance/isaac-ng.exe")
    launcher=GameLauncher(ROOT,log,pause,slot=1,timeout=75)
    session=CombatSession(SocketBridge(),None,pause,log,ready_timeout=75,new_run=True,seed=args.seed,
                          skill_store=ROOT/"skills" if args.charge else None)
    if args.charge and not isinstance(session.build_resolver.mechanism_adapter,AdaptiveChargedBuild):
        raise RuntimeError("ADAPTIVE_69_NOT_LOADED")
    ids=([69] if args.charge else [])+args.attributes
    async def initialize(current):
        s=current.observation.latest
        if not s or s.hostiles or not s.room.get("is_starting_room"):raise RuntimeError("ITEM_SETUP_NOT_CLEAR_START")
        for item in ids:
            log("initialization_command",command=f"giveitem c{item}",source_mode="development_initialization")
            await current.rpc("EXEC_CONSOLE",{"command":f"giveitem c{item}"})
            deadline=time.monotonic()+5
            while not (s.inventory.get("collectibles") or {}).get(str(item)):
                if time.monotonic()>deadline:raise TimeoutError("ITEM_INVENTORY_NOT_OBSERVED")
                s=await current.next_snapshot(after=s.snapshot_id)
        log("item_matrix_initialization_complete",frame=s.frame,inventory=s.inventory,stats=s.stats)
    print("Item matrix artifacts: "+str(directory),flush=True)
    try:
        report=await session.run(bootstrap=launcher,on_ready=initialize)
        final=session.observation.latest
        result={"game_build":"1.7.9b.J835","seed":args.seed,"source_mode":"development_initialization",
                "items":ids,"inventory_confirmed":bool(final and all((final.inventory.get("collectibles") or {}).get(str(i)) for i in ids)),
                "result":report.result,"damage_half_hearts":report.damage_half_hearts,
                "combat_ticks":report.combat_ticks,"wall_seconds":report.wall_seconds,"error":report.error}
        if args.charge:
            adapter=session.build_resolver.mechanism_adapter
            result.update(program_sha256=adapter.program.sha256,calibration_id=adapter.current_calibration["calibration_id"],
                          calibration_source=adapter.current_calibration["source"],
                          calibration_scope=adapter.current_calibration["scope_key"],
                          **proof_from_trace(directory,adapter.program.sha256))
            result["passed"]=bool(report.success and result["inventory_confirmed"] and
                                  result["applied_releases"] and result["new_tears_observed"] and not result["mechanism_failures"])
        else:
            result["passed"]=bool(report.success and result["inventory_confirmed"] and report.combat_ticks>0)
        (directory/"item-matrix-trial.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
        return 0 if result["passed"] else 1
    finally:launcher.collect_evidence()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--charge",action="store_true",help="include 69 and its published program")
    parser.add_argument("--attributes",type=int,nargs="*",default=[],choices=(1,4,12,20,27,32,50))
    parser.add_argument("--seed",default="3BYH A82R")
    args=parser.parse_args()
    if not args.charge and not args.attributes:parser.error("specify --charge or --attributes")
    raise SystemExit(asyncio.run(run(args)))
