"""Development initialization -> model skill normal combat -> independent proof.

Attach to an already bootstrapped J835 game. No API calls and no GUI input.
The precompiled program uses a local room plan; native room planning pause is
unnecessary for this fixture. This does not validate online room planning.
"""
import argparse
import asyncio
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"SocketBridge/python"))
from facade import SocketBridge
from combat.catalog import default_plan
from combat.runtime import CombatSession,RunLog
from combat.observation import CombatJudge
from mechanisms.store import SkillStore,read_candidate
from mechanisms.weapon import ChargedBuild,register_program


class PreparedSkillSession(CombatSession):
    async def configure_room(self,s):
        await self.release()
        await self.set_mode("FORCE_AI")
        current=await self.next_snapshot(after=s.snapshot_id)
        build=self.build_resolver.update(current)
        self.plan=default_plan(self.context_ref(current,build),family=build.family)
        self.log("plan_installed",source="local_precompiled_skill",plan=self.plan.model_dump())
        return current


def proof_from_trace(directory,program_sha):
    adopted=set();released=set();applied=set();accepted=set();tears={};max_fuel=0;max_ms=0.;failures=[]
    with (directory/"trace.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row=json.loads(line)
            if row["kind"]=="action_sent":
                diagnostics=(row.get("metadata") or {}).get("diagnostics") or {}
                mechanism=diagnostics.get("mechanism") or {}
                if mechanism.get("program_sha256")==program_sha:
                    adopted.add(row["action_id"])
                    if diagnostics.get("fire_reason")=="program_release" and row["shoot"]==[0,0]:released.add(row["action_id"])
                    max_fuel=max(max_fuel,mechanism.get("max_fuel",0));max_ms=max(max_ms,mechanism.get("max_call_ms",0))
            if row["kind"]=="component_fallback":failures.append(row)
            if row["kind"]!="game_message":continue
            message=row.get("message") or {}
            if message.get("type")=="ACTION_RECEIPT" and message.get("accepted"):accepted.add(message["action_id"])
            s=(message.get("payload") or {}).get("COMBAT_FRAME")
            if not s:continue
            for feedback in (s.get("weapon_feedback") or {}).get("history") or []:
                if feedback.get("consistent") is True and all((feedback.get("queried") or {}).get(k) is True for k in ("left","right","up","down")):
                    if not any((feedback.get("values") or {}).values()):applied.add(feedback.get("action_id"))
            for tear in (s.get("projectiles") or {}).get("player_tears") or []:
                if tear.get("owner_type")==1 and tear.get("entity_frame_count")==0:
                    tears[tear["instance_id"]]={"frame":s["frame"],"damage":tear.get("collision_damage"),"velocity":tear["vel"]}
    return {"adopted_actions":len(adopted),"released_actions":len(released),"applied_releases":len(released&applied&accepted),
            "new_tears_observed":len(tears),"tear_samples":dict(list(tears.items())[:64]),"mechanism_failures":failures,
            "max_frame_fuel":max_fuel,"max_program_call_ms":max_ms}


async def run(args):
    if args.candidate:
        program,calibration,provenance,_=read_candidate(args.candidate)
        origin="trial"
    else:
        active=SkillStore(ROOT/"skills").active()
        if active is None:raise ValueError("NO_PUBLISHED_SKILL")
        program,calibration,manifest=active;provenance=manifest["provenance"];origin="reused"
    # Comparing the installed source prevents attaching a probe to an older mod
    # file. Bootstrapping is responsible for loading these files in a new game.
    installed=ROOT/"The Binding of Isaac Rebirth Repentance/mods/SocketBridge/main.lua"
    if installed.read_bytes()!=(ROOT/"SocketBridge/main.lua").read_bytes():raise RuntimeError("INSTALLED_LUA_DIFFERS_FROM_SOURCE")
    directory=ROOT/"artifacts/runs"/datetime.now(timezone.utc).strftime("m2b-"+origin+"-%Y%m%dT%H%M%S%fZ")
    log=RunLog(directory);log.manifest(ROOT,provenance.get("response_model"))
    session=PreparedSkillSession(SocketBridge(),None,None,log,new_run=True,seed=args.seed)
    register_program(program,calibration)
    session.build_resolver.mechanism_adapter=ChargedBuild(program,calibration)
    log("skill_trial_authority",origin=origin,program_sha256=program.sha256,scope=calibration["scope"],provenance=provenance)

    async def initialize(current):
        current.task_started=False
        initial=await current.next_snapshot()
        if initial.blocking_combat or not initial.room.get("is_starting_room"):raise RuntimeError("FIXTURE_REQUIRES_CLEAR_START")
        log("initialization_command",command="giveitem c69",source_mode="development_initialization")
        await current.rpc("EXEC_CONSOLE",{"command":"giveitem c69"})
        deadline=time.monotonic()+5;s=initial
        while not (s.inventory.get("collectibles") or {}).get("69"):
            if time.monotonic()>deadline:raise TimeoutError("ITEM_NOT_OBSERVED")
            s=await current.next_snapshot(after=s.snapshot_id)
        if s.health!=initial.health or s.blocking_combat:raise RuntimeError("UNSAFE_INITIALIZATION")
        current.build_resolver.update(s)
        current.task_start_frame=s.frame;current.task_judge=CombatJudge();current.task_judge.observe(s)
        current.task_started=True
        log("mechanism_initialization_complete",frame=s.frame,inventory=s.inventory,stats=s.stats,health=s.health)

    print("Mechanism artifacts: "+str(directory),flush=True)
    report=await session.run(on_ready=initialize)
    proof={"schema_version":1,"program_sha256":program.sha256,"origin":origin,"generation_api_calls":0,
        "normal_combat":True,"source_mode":"development_initialization","game_build":"1.7.9b.J835",
        "calibration_id":calibration["calibration_id"],"seed":args.seed,**proof_from_trace(directory,program.sha256)}
    proof["passed"]=bool(report.success and proof["applied_releases"] and proof["new_tears_observed"] and not proof["mechanism_failures"])
    if origin=="reused" and proof["mechanism_failures"]:
        SkillStore(ROOT/"skills").quarantine(program.sha256,"runtime_component_failure",directory)
    (directory/"mechanism-trial.json").write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"directory":str(directory),"report":report.model_dump(),"mechanism":proof},ensure_ascii=True,indent=2),flush=True)
    return 0 if proof["passed"] else 1


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    choice=parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--candidate",type=Path)
    choice.add_argument("--reuse",action="store_true")
    parser.add_argument("--seed",default="3BYH A82R")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
