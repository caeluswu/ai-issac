"""Development-only item fixture -> normal pickup -> first-room combat.

Console authority exists only in the recorded initialization hook. This is not
a normal-run success metric, a floor controller or an active learning probe.
"""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"SocketBridge/python"))
from facade import SocketBridge
from combat.catalog import default_plan
from combat.controller import JointActionSolver
from combat.geometry import GeometryService
from combat.item_knowledge import KnowledgeGateway
from combat.item_learning import ItemLearning
from combat.launcher import GameLauncher
from combat.models import Vec
from combat.runtime import CombatSession, NativePause, RunLog


async def main():
    log=RunLog(ROOT/"artifacts/runs"/datetime.now(timezone.utc).strftime("m2-preparation-%Y%m%dT%H%M%S%fZ"))
    log.manifest(ROOT,None)
    learning=ItemLearning(KnowledgeGateway.from_config(ROOT),source_mode="development_initialization")
    pause=NativePause(game_executable=ROOT/"The Binding of Isaac Rebirth Repentance/isaac-ng.exe")
    launcher=GameLauncher(ROOT,log,pause,slot=1,timeout=75,require_baseline=True)
    seed=json.loads((ROOT/"configs/task/phase1-navigation-seeds.json").read_text(encoding="utf-8"))["seeds"][0]
    session=CombatSession(SocketBridge(),None,pause,log,new_run=True,seed=seed,learning=learning)
    proof={"source_mode":"development_initialization","seed":seed,"seed_source":"phase1-navigation-seeds.json[0]",
           "paid_api_calls":0,"active_experiments":False}

    async def initialize(current):
        s=await current.next_snapshot()
        if not s.room.get("is_starting_room") or s.blocking_combat: raise RuntimeError("FIXTURE_REQUIRES_CLEAR_START")
        geometry=GeometryService(s)
        spawn=s.player.pos
        goals=[spawn+Vec(x=dx,y=dy) for dx,dy in ((100,0),(-100,0),(0,100),(0,-100))]
        goal=next((p for p in goals if geometry.clear(p) and geometry.path(s.player.pos,p)),None)
        if goal is None: raise RuntimeError("NO_SAFE_INITIALIZATION_POSITION")
        solver=JointActionSolver(); await current.set_mode("FORCE_AI")
        start=s.frame
        while s.player.pos.distance(goal)>18:
            if s.frame-start>90 or s.blocking_combat: raise RuntimeError("FIXTURE_POSITION_TIMEOUT_OR_THREAT")
            build=current.build_resolver.update(s)
            action=await asyncio.to_thread(solver.solve,s,default_plan(current.context_ref(s,build)),build,True,goal)
            try: await current.send_action(s,action.move,(0,0),action.model_dump())
            except TimeoutError as exc:
                if "STALE_ACTION" not in str(exc): raise
            s=await current.next_snapshot(after=s.snapshot_id)
        await current.release(); await current.set_mode("MANUAL")
        log("initialization_command",command="spawn 5.100.1",source_mode="development_initialization")
        await current.rpc("EXEC_CONSOLE",{"command":"spawn 5.100.1"})
        deadline=time.monotonic()+5
        target=None
        while (target is None or target.wait != 0) and time.monotonic()<deadline:
            s=await current.next_snapshot(after=s.snapshot_id)
            target=next((p for p in s.pickups or [] if p.variant==100 and p.sub_type==1),None)
        if target is None: raise RuntimeError("FIXTURE_ITEM_NOT_OBSERVED")
        build=current.build_resolver.update(s)
        await learning.prepare_room(s,build)
        decision=learning.decisions[(target.instance_id,target.sub_type,target.price)]
        picked=await learning.pickups.execute(current,s,build,target,decision)
        log("preparation_pickup",decision_id=decision.decision_id,proof=picked)
        proof["pickup"]=picked
        if picked["result"]!="confirmed": raise RuntimeError("FIXTURE_PICKUP_UNCONFIRMED")

    print("Preparation artifacts: "+str(log.directory),flush=True)
    try:
        report=await session.run(bootstrap=launcher,on_ready=initialize)
        proof.update(report=report.model_dump(),dataset_id=learning.gateway.package.dataset_id)
        build=session.build_resolver.update(session.observation.latest)
        proof["final_build"]=build.model_dump()
        proof["experience"]=learning.gateway.store.retrieve(build.calibration_key)
        (log.directory/"preparation-summary.json").write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"result":report.result,"error":report.error,"pickup":proof.get("pickup",{}).get("result"),
                          "damage":report.damage_half_hearts,"calibration_source":build.calibration_source,
                          "calibration_samples":build.calibration_samples,"experience_revision":proof["experience"]["experience_revision"]},indent=2),flush=True)
        return 0 if report.success and proof.get("pickup",{}).get("result")=="confirmed" else 1
    finally: launcher.collect_evidence()


if __name__=="__main__": raise SystemExit(asyncio.run(main()))
