"""J835 knife observation, finite probe, model trial and trace proof.

Development setup grants item 114 before the measured task. Combat still uses
ordinary movement/shoot inputs. --probe-only makes no model request.
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
from combat.item_knowledge import KnowledgeGateway
from combat.item_learning import ItemLearning
from combat.geometry import GeometryService
from combat.controller import JointActionSolver
from combat.catalog import default_plan
from combat.models import Vec
from mechanisms.knife import KnifeBuild,register_knife
from mechanisms.knife_calibration import probe_knife
from mechanisms.item_adaptation import ItemAdaptation
from mechanisms.knife_evidence import summarize
from mechanisms.knife_developer import AttackStore
from mechanisms.javascript import AttackModelRuntime


async def run(args):
    directory=ROOT/"artifacts/runs"/datetime.now(timezone.utc).strftime("m2c-knife-%Y%m%dT%H%M%S%fZ")
    log=RunLog(directory);log.manifest(ROOT,None)
    pause=NativePause(game_executable=ROOT/"The Binding of Isaac Rebirth Repentance/isaac-ng.exe")
    launcher=GameLauncher(ROOT,log,pause,slot=1,timeout=75)
    session=CombatSession(SocketBridge(),None,pause,log,ready_timeout=75,new_run=True,seed=args.seed,skill_store=ROOT/"skills")
    register_knife()
    if args.probe_only:
        session.build_resolver.knife_enabled=True
        adapter=KnifeBuild(ROOT/"artifacts/item-adaptation/knife-calibrations")
        session.build_resolver.adapters[-1]=adapter
    else:
        gateway=KnowledgeGateway.from_config(ROOT)
        if args.pedestal:
            session.learning=ItemLearning(gateway,source_mode="development_initialization")
            gateway.build_resolver=session.build_resolver
        session.item_adaptation=ItemAdaptation(ROOT,gateway,allow_generate=not args.reuse)
        if args.candidate:
            package=AttackStore(ROOT/"artifacts/item-adaptation/development/moms-knife").load(args.candidate)
            if session.item_adaptation.runtime:session.item_adaptation.runtime.close()
            session.item_adaptation.package=package
            session.item_adaptation.runtime=AttackModelRuntime(package[0])
        if args.reuse and not session.item_adaptation.runtime:raise RuntimeError("NO_PUBLISHED_KNIFE_COMPONENT")
    async def initialize(current):
        s=current.observation.latest
        if not s or s.hostiles or not s.room.get("is_starting_room"):raise RuntimeError("KNIFE_SETUP_NOT_CLEAR_START")
        for item in args.attributes:
            log("initialization_command",command=f"giveitem c{item}",source_mode="development_initialization")
            await current.rpc("EXEC_CONSOLE",{"command":f"giveitem c{item}"})
            s=await current.next_snapshot(after=s.snapshot_id)
        if args.pedestal:
            await current.item_adaptation.prepare(current,s)
            geometry=GeometryService(s);goals=[s.player.pos+Vec(x=dx,y=dy) for dx,dy in ((100,0),(-100,0),(0,100),(0,-100))]
            goal=next((p for p in goals if geometry.clear(p) and geometry.path(s.player.pos,p)),None)
            if goal is None:raise RuntimeError("NO_SAFE_PEDESTAL_POSITION")
            solver=JointActionSolver();await current.set_mode("FORCE_AI");began=s.frame
            while s.player.pos.distance(goal)>18:
                if s.frame-began>90 or s.hostiles:raise RuntimeError("PEDESTAL_POSITION_FAILED")
                b=current.build_resolver.update(s)
                action=await asyncio.to_thread(solver.solve,s,default_plan(current.context_ref(s,b)),b,True,goal)
                await current.send_action(s,action.move,(0,0),action.model_dump())
                s=await current.next_snapshot(after=s.snapshot_id)
            await current.release();await current.set_mode("MANUAL")
            log("initialization_command",command="spawn 5.100.114",source_mode="development_initialization")
            await current.rpc("EXEC_CONSOLE",{"command":"spawn 5.100.114"})
            deadline=time.monotonic()+5;target=None
            while (target is None or target.wait!=0) and time.monotonic()<deadline:
                s=await current.next_snapshot(after=s.snapshot_id)
                target=next((p for p in s.pickups or [] if p.variant==100 and p.sub_type==114),None)
            if target is None:raise RuntimeError("KNIFE_PEDESTAL_NOT_OBSERVED")
            b=current.build_resolver.update(s)
            b=await current.learning.apply(s,current.build_resolver,b)
            await current.learning.prepare_room(s,b)
            decision=current.learning.decisions[(target.instance_id,target.sub_type,target.price)]
            picked=await current.learning.pickups.execute(current,s,b,target,decision)
            log("knife_pickup_proof",decision_id=decision.decision_id,proof=picked)
            current.knife_pickup_proof=picked
            if picked["result"]!="confirmed":raise RuntimeError("KNIFE_PICKUP_UNCONFIRMED: "+str(picked["reason"]))
            s=current.observation.latest
        else:
            log("initialization_command",command="giveitem c114",source_mode="development_initialization")
            await current.rpc("EXEC_CONSOLE",{"command":"giveitem c114"})
        deadline=time.monotonic()+8
        while True:
            s=await current.next_snapshot(after=s.snapshot_id)
            if (s.inventory.get("collectibles") or {}).get("114") and s.weapons and s.weapons.types==[4]:break
            if time.monotonic()>deadline:raise TimeoutError("KNIFE_WEAPON_OBSERVATION_MISSING")
        log("knife_initialization_complete",frame=s.frame,weapon_types=s.weapons.types,
            inventory=s.inventory,observed_knives=s.projectiles.get("player_knives") or [])
    print("Knife artifacts: "+str(directory),flush=True)
    try:
        if args.probe_only:
            initial=await session.open(bootstrap=launcher,on_ready=initialize)
            try:
                calibration,_=await probe_knife(session,initial)
                if calibration:adapter.adopt(calibration)
                result={"calibration":calibration,"passed":bool(calibration and calibration["completed_cycles"]>=3 and calibration["observability"]["knife_entities"]),
                        "artifacts":str(directory)}
                (directory/"knife-probe.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
            finally:await session.close()
            log.file.close()
            print(json.dumps({k:v for k,v in result.items() if k!="calibration"},ensure_ascii=False),flush=True)
            return 0 if result["passed"] else 1
        report=await session.run(bootstrap=launcher,on_ready=initialize)
        adaptation=session.item_adaptation
        package=adaptation.package
        source_sha=package[1]["source_sha256"] if package else None
        proof=summarize(directory/"trace.jsonl",source_sha,report,adaptation.adapter.calibration)
        proof.update(source_mode="development_initialization",seed=args.seed,
                     package_status=package[1]["status"] if package else None,origin="reused" if args.reuse else "generated_or_cached")
        proof["attributes"]=args.attributes
        proof["normal_pickup_confirmed"]=getattr(session,"knife_pickup_proof",{}).get("result")=="confirmed"
        final_items=(session.observation.latest.inventory.get("collectibles") or {}) if session.observation.latest else {}
        proof["attribute_inventory_confirmed"]=all(final_items.get(str(item),0)>0 for item in args.attributes)
        proof["calibration_reused"]=adaptation.adapter.from_cache
        proof["program_reused"]=bool(package and package[1]["status"]=="usable" and
                                      proof["source_sha256"]==package[1]["source_sha256"])
        if args.reuse:
            proof["reuse_passed"]=bool(proof["program_reused"] and adaptation.adapter.from_cache and proof["result"]=="CLEARED" and
                proof["model_actions"]>0 and proof["confirmed_knife_hits_total"]>0 and
                not proof["component_failures"])
        if args.attributes:
            proof["attribute_trial_passed"]=bool(proof["attribute_inventory_confirmed"] and
                proof["program_reused"] and proof["completed_probe_cycles"]>=3 and
                proof["result"]=="CLEARED" and proof["observed_flight_starts"]>0 and
                proof["confirmed_knife_hits_total"]>0 and proof["model_actions"]>0 and
                not proof["component_failures"])
        (directory/"knife-trial.json").write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding="utf-8")
        if package and package[1]["status"]=="trial" and adaptation.allow_generate and not args.candidate:
            def feedback_log(kind,**value):
                with (directory/"item-feedback.jsonl").open("a",encoding="utf-8") as output:
                    output.write(json.dumps({"kind":kind,**value},ensure_ascii=False)+"\n")
            budget=adaptation.model_client().budget.report()
            if budget["gap_requests"]<3 and budget["phase_requests"]<6:
                proof["feedback"]=await adaptation.review_trial(proof,feedback_log)
            else:
                proof["feedback"]={"decision":"deferred","reason":"M2C_KNIFE_REQUEST_LIMIT_REACHED","budget":budget}
            (directory/"knife-trial.json").write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"report":report.model_dump(),"knife":proof,"artifacts":str(directory)},ensure_ascii=False,indent=2),flush=True)
        accepted=(proof["attribute_trial_passed"] if args.attributes else proof["reuse_passed"] if args.reuse else proof["passed"])
        return 0 if accepted else 1
    finally:launcher.collect_evidence()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-only",action="store_true",help="observe knife and three cycles, no API")
    parser.add_argument("--reuse",action="store_true",help="load a published model, no API")
    parser.add_argument("--candidate",type=Path,help="trial a saved model package without API or publication")
    parser.add_argument("--pedestal",action="store_true",help="spawn and normally pick up 114 in the clear start room")
    parser.add_argument("--attributes",type=int,nargs="*",default=[],choices=(1,4,12,20,27,32,50),
                        help="development setup grants supported attribute/flight items before 114")
    parser.add_argument("--seed",default="3BYH A82R")
    args=parser.parse_args()
    if args.probe_only and (args.reuse or args.candidate):parser.error("probe-only is separate from candidate/reuse")
    if args.probe_only and (args.pedestal or args.attributes):parser.error("probe-only uses base 114")
    raise SystemExit(asyncio.run(run(args)))
