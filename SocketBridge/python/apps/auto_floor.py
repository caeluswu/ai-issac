"""Automatically launch vanilla J835 and complete a bounded normal first floor."""
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
from campaign.director import FloorDirector
from campaign.planning import FloorPlanner

ROOT = Path(__file__).resolve().parents[3]


async def main(args):
    entry_started=time.monotonic()
    load_env(ROOT/".env.local",override=True)
    budget = RequestBudget(max_requests=0,max_routine=0,max_diagnostic=0) if args.local_only else RequestBudget(max_requests=args.llm_requests,max_routine=min(4,args.llm_requests),max_diagnostic=max(0,args.llm_requests-4))
    client = None if args.local_only else DeepSeekClient(timeout=45,budget=budget)
    stamp = datetime.now(timezone.utc).strftime("floor-%Y%m%dT%H%M%S%fZ")
    log = RunLog(args.output_dir or ROOT/"artifacts/runs"/stamp)
    log.manifest(ROOT,client.model if client else None)
    package = KnowledgeGateway.from_config(ROOT)
    package.purchase_budget = None
    learning = ItemLearning(package)
    planner = FloorPlanner(Planner(client,KnowledgeStore(ROOT/"configs/knowledge"),log),log) if client else None
    pause = NativePause(game_executable=ROOT/"The Binding of Isaac Rebirth Repentance/isaac-ng.exe")
    launcher = GameLauncher(ROOT,log,pause,slot=args.slot,timeout=args.ready_timeout,require_baseline=True)
    session = CombatSession(SocketBridge(),planner,pause,log,ready_timeout=args.ready_timeout,new_run=True,seed=args.seed,learning=learning,skill_store=ROOT/"skills")
    director = FloorDirector(session,budget)
    print("Floor artifacts: "+str(log.directory),flush=True)
    report = None
    try:
        manifest_path = log.directory/"manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(task="M2-A",source_mode="normal_play",evaluation_mode="debug_replay" if args.seed else "normal_random",seed_requested=args.seed,
            support_profile="j835-isaac-normal-tears-first-floor-v1",knowledge_dataset=package.package.dataset_id,
            request_budget=budget.report(),save_baseline="configs/task/save-baseline.slot-1.json")
        manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
        report = await director.run(bootstrap=launcher)
        report.process_wall_seconds=time.monotonic()-entry_started
        report.evaluation_mode="debug_replay" if args.seed else "normal_random"
        (log.directory/"floor-report.json").write_text(report.model_dump_json(indent=2),encoding="utf-8")
        with (log.directory/"room-results.jsonl").open("w",encoding="utf-8") as rows:
            for row in report.rooms: rows.write(json.dumps(row,ensure_ascii=False)+"\n")
        log("floor_report",**report.model_dump())
    finally:
        launcher.collect_evidence()
        if client: await client.aclose()
        log.file.close()
        with (log.directory/"decisions.jsonl").open("w",encoding="utf-8") as out:
            for line in (log.directory/"trace.jsonl").open(encoding="utf-8"):
                row = json.loads(line)
                if row["kind"] in {"floor_objective","floor_transition","pickup_transaction","plan_installed","plan_template_rebound","floor_request_budget_exhausted","floor_failed"}:
                    out.write(line)
    print(report.model_dump_json(indent=2),flush=True)
    return 0 if report.success and (args.local_only or report.llm_configured) else 1


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot",type=int,choices=(1,2,3),default=1)
    parser.add_argument("--local-only",action="store_true")
    parser.add_argument("--llm-requests",type=int,choices=range(1,7),default=6,help="lower the total HTTP attempt limit; default 6")
    parser.add_argument("--seed",help="debug replay only; omitted means a normal random run")
    parser.add_argument("--ready-timeout",type=float,default=75)
    parser.add_argument("--output-dir",type=Path)
    args = parser.parse_args()
    if args.seed and not re.fullmatch(r"[A-Z0-9]{4} [A-Z0-9]{4}",args.seed): parser.error("seed format is XXXX XXXX")
    if args.ready_timeout <= 0: parser.error("ready-timeout must be positive")
    return asyncio.run(main(args))


if __name__ == "__main__": raise SystemExit(cli())
