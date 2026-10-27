"""Offline damage extraction, budgeted DeepSeek summaries and separate reviews."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/"SocketBridge/python"))
from combat.agent_store import AgentStore
from combat.damage import extract_trace,encoded
from combat.damage_summary import summarize,revalidate_saved,saved_for_review
from combat.exploration_budget import ExplorationBudget
from combat.planner import DeepSeekClient,load_env

ARTIFACTS=ROOT/"artifacts/damage-attribution"


async def run_summary(args,store):
    paths=sorted(Path(args.directory).rglob("*.analysis.json"))
    values={a["episode_id"]:a for p in paths for a in [json.loads(p.read_text(encoding="utf-8"))]}
    selected=[values[key] for key in sorted(values) if not args.episode or key in args.episode]
    if not selected or len(selected)>8:raise ValueError("Select 1..8 episodes; use --episode to narrow the batch")
    if args.episode and set(args.episode)!={a["episode_id"] for a in selected}:raise ValueError("UNKNOWN_EPISODE_ID")
    budget=ExplorationBudget(ARTIFACTS/"budget.sqlite")
    load_env(ROOT/".env.local",override=True)
    client=DeepSeekClient(timeout=120,budget=budget,max_tokens=4096,retries=1,thinking="disabled")
    try:result=await summarize(selected,client,ARTIFACTS/"summaries",store,retry_failed=args.retry_failed)
    finally:await client.aclose()
    return {**result,"budget_now":budget.report()}


def cli():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command",required=True)
    extract=sub.add_parser("extract");extract.add_argument("traces",nargs="+");extract.add_argument("--output",type=Path,default=ARTIFACTS/"extracted")
    summary=sub.add_parser("summarize");summary.add_argument("directory",type=Path)
    summary.add_argument("--episode",action="append");summary.add_argument("--retry-failed",action="store_true")
    sub.add_parser("budget")
    revalidate=sub.add_parser("revalidate");revalidate.add_argument("summary_id")
    review=sub.add_parser("record-review");review.add_argument("path",type=Path)
    args=parser.parse_args()
    store=AgentStore(ROOT/"artifacts/agent-state/agent.sqlite")
    try:
        if args.command=="extract":
            result=[]
            for item in args.traces:
                path=Path(item);sha=hashlib.sha256(path.read_bytes()).hexdigest()
                result.append(extract_trace(path,args.output/sha[:16],store_path=store.path))
        elif args.command=="summarize":result=asyncio.run(run_summary(args,store))
        elif args.command=="budget":result=ExplorationBudget(ARTIFACTS/"budget.sqlite").report()
        elif args.command=="revalidate":result=revalidate_saved(args.summary_id,ARTIFACTS/"summaries",store)
        else:
            value=json.loads(args.path.read_text(encoding="utf-8"))
            original=store.get("damage_summary",value["summary_id"])
            if not original:original=saved_for_review(value["summary_id"],ARTIFACTS/"summaries",store)
            expected={r["episode_id"] for r in original["response"]["episodes"]}
            if value.get("reviewer")!="development_agent" or {r["episode_id"] for r in value["episodes"]}!=expected or len(value["episodes"])!=len(expected):raise ValueError("REVIEW_EPISODE_SET_MISMATCH")
            analyses={a["episode_id"]:a for aid in original["analysis_ids"] for a in [store.get("damage_analysis",aid)] if a}
            for row in value["episodes"]:
                if row["verdict"] not in ("reasonable","partly_reasonable","insufficient_evidence","incorrect") or not row.get("reason") or not row.get("evidence_refs"):
                    raise ValueError("INVALID_DEVELOPMENT_REVIEW")
                if row["episode_id"] not in analyses or not set(row["evidence_refs"]).issubset(analyses[row["episode_id"]]["evidence"]):
                    raise ValueError("INVALID_REVIEW_EVIDENCE_REFERENCE")
            identity=hashlib.sha256(encoded(value).encode()).hexdigest()
            store.record("damage_review",identity,value);result={"review_id":identity,"recorded":True}
        if sys.stdout.encoding and sys.stdout.encoding.lower()!="utf-8":sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 1 if isinstance(result,dict) and result.get("status") in ("failed","interrupted") else 0
    except (ValueError,OSError,RuntimeError) as exc:
        print(json.dumps({"status":"error","error_type":type(exc).__name__,"message":str(exc)},ensure_ascii=True));return 1


if __name__=="__main__":raise SystemExit(cli())
