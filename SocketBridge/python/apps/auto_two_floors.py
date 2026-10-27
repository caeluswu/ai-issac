"""Run J835 Isaac through a selected mainline floor on one normal game run."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from facade import SocketBridge
from combat.launcher import GameLauncher
from combat.planner import DeepSeekClient, KnowledgeStore, Planner, load_env
from combat.runtime import CombatSession, NativePause, RunLog
from combat.item_knowledge import KnowledgeGateway
from combat.item_learning import ItemLearning
from campaign.budget import RequestBudget
from campaign.planning import FloorPlanner
from campaign.run_director import RunDirector
from mechanisms.item_adaptation import ItemAdaptation

ROOT = Path(__file__).resolve().parents[3]


async def main(args):
    load_env(ROOT / ".env.local", override=True)
    budget = RequestBudget(max_requests=args.llm_requests,
                           max_routine=min(4,args.llm_requests),
                           max_diagnostic=max(0,args.llm_requests-4))
    client = DeepSeekClient(timeout=45,budget=budget) if args.llm_requests else None
    stamp = datetime.now(timezone.utc).strftime("run-mainline-%Y%m%dT%H%M%S%fZ")
    log = RunLog(args.output_dir or ROOT / "artifacts/runs" / stamp)
    log.manifest(ROOT,client.model if client else None)
    gateway = KnowledgeGateway.from_config(ROOT)
    gateway.purchase_budget = 0
    learning = ItemLearning(gateway)
    planner = FloorPlanner(Planner(client,KnowledgeStore(ROOT / "configs/knowledge"),log),log) if client else None
    pause = NativePause(game_executable=ROOT / "The Binding of Isaac Rebirth Repentance/isaac-ng.exe")
    launcher = GameLauncher(ROOT,log,pause,slot=args.slot,timeout=args.ready_timeout,require_baseline=True)
    session = CombatSession(SocketBridge(),planner,pause,log,ready_timeout=args.ready_timeout,
                            new_run=True,seed=args.seed,learning=learning,skill_store=ROOT / "skills")
    session.item_adaptation = ItemAdaptation(ROOT,gateway,allow_generate=False)
    director = RunDirector(session,budget,target_stage=args.target_stage,wall_limit=args.wall_limit)
    manifest_path = log.directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(task="M3-A" if args.target_stage==2 else "M3-B",source_mode="normal_play",
                    evaluation_mode="debug_replay" if args.seed else "normal_random",
                    seed_requested=args.seed,target_stage=args.target_stage,request_budget=budget.report(),
                    save_baseline="configs/task/save-baseline.slot-1.json")
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Mainline artifacts: "+str(log.directory),flush=True)
    began=time.monotonic()
    try:
        report=await director.run(bootstrap=launcher)
        report.wall_seconds=time.monotonic()-began
        (log.directory/"run-report.json").write_text(report.model_dump_json(indent=2),encoding="utf-8")
        log("run_report",**report.model_dump())
        print(report.model_dump_json(indent=2),flush=True)
        return 0 if report.success and (not client or any(f.llm_configured for f in report.floors)) else 1
    finally:
        launcher.collect_evidence()
        if client:await client.aclose()
        log.file.close()


def cli():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot",type=int,choices=(1,2,3),default=1)
    parser.add_argument("--target-stage",type=int,choices=(2,3,4),default=2,
                        help="normal exit after this stage; default two floors")
    parser.add_argument("--llm-requests",type=int,choices=range(0,7),default=0)
    parser.add_argument("--seed",help="debug replay only; omitted means a normal random run")
    parser.add_argument("--ready-timeout",type=float,default=75)
    parser.add_argument("--wall-limit",type=float,default=3600)
    parser.add_argument("--output-dir",type=Path)
    args=parser.parse_args()
    if args.seed and not re.fullmatch(r"[A-Z0-9]{4} [A-Z0-9]{4}",args.seed):parser.error("seed format is XXXX XXXX")
    if args.ready_timeout<=0 or args.wall_limit<=0:parser.error("timeouts must be positive")
    return asyncio.run(main(args))


if __name__=="__main__":raise SystemExit(cli())
