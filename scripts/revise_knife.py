"""One trace-grounded DeepSeek function revision; no game or UI action."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"SocketBridge/python"))
from combat.damage import encoded
from combat.exploration_budget import ExplorationBudget
from combat.planner import DeepSeekClient,load_env
from mechanisms.knife_developer import AttackStore,save_model_patch


async def run(args):
    trial=args.trial.resolve()
    if not trial.is_relative_to((ROOT/"artifacts/runs").resolve()):raise ValueError("TRIAL_OUTSIDE_RUN_ARTIFACTS")
    evidence=json.loads((trial/"knife-trial-reanalyzed.json").read_text(encoding="utf-8"))
    root=ROOT/"artifacts/item-adaptation/development/moms-knife"
    pointer=json.loads((root/"candidate.json").read_text(encoding="utf-8"))["directory"]
    source,manifest,candidate=AttackStore(root).load(pointer)
    if evidence.get("source_sha256")!=manifest["source_sha256"] or evidence.get("result")!="CLEARED":
        raise ValueError("REVISION_TRACE_SOURCE_MISMATCH")
    budget=ExplorationBudget(ROOT/"artifacts/damage-attribution/budget.sqlite",phase="item_adaptation",gap="moms_knife")
    if args.grant_authorization:
        budget.grant_gap(args.grant_authorization)
    limits=budget.report()
    if limits["gap_requests"]>=limits["gap_request_limit"]:raise RuntimeError("KNIFE_REVISION_BUDGET_EXHAUSTED")
    request={"source":source,"source_sha256":manifest["source_sha256"],
        "trial":{k:evidence.get(k) for k in ("result","combat_ticks","damage_half_hearts","model_actions",
                 "observed_flight_starts","observed_returns","confirmed_thrown_hits","launch_samples")},
        "confirmed_J835_behavior":{"raw_entity_charge":"0 while held, changes at release",
            "model_input_charge":"estimated from consecutive engine-queried shooting frames and calibrated charge-per-update; engine_charge retains raw value",
            "knife_velocity":"scalar; ordinary entity velocity stayed zero"},
        "required_patch":"Replace only the complete function proposeBatch. Score candidate movement by predicted cardinal knife lane and target lateral offset, and avoid rewarding release when the lateral miss or predicted motion makes a hit unlikely. Retain finite charging and safe opportunities to attack. Use actual enemy radius and observed knife width/range. Keep explicit state and SDK shape. Normal action solver handles collision safety. Do not claim a guaranteed hit."}
    messages=[{"role":"system","content":"You are revising the provided DeepSeek-authored Mom's Knife JS component after a real J835 combat trial. Treat the trace as observations, not a guarantee of causality. Return exactly JSON {function:'proposeBatch',replacement:'complete JavaScript function...',summary:'short explanation'}. Write a full replacement for that one named function, reusing existing helpers. No Markdown, imports, host IO or changes to other functions. Keep 1..9 candidate IDs, output fields and mode/shoot constraints."},
              {"role":"user","content":encoded(request)}]
    path=root/"revision-request.json"
    if path.exists():raise FileExistsError("REVISION_REQUEST_ALREADY_EXISTS; inspect it before retrying")
    path.write_text(encoded({"messages":messages,"source_sha256":manifest["source_sha256"],"trial":str(trial)})+"\n",encoding="utf-8")
    load_env(ROOT/".env.local",override=True)
    client=DeepSeekClient(timeout=120,budget=budget,max_tokens=3500,retries=1,thinking="disabled")
    try:
        raw,usage=await client.complete(messages)
        (root/"revision-response.json").write_bytes(raw.encode())
        (root/"revision-usage.json").write_text(encoded(usage)+"\n",encoding="utf-8")
        patched=save_model_patch((source,manifest,candidate),raw,usage,root)
        (root/"candidate.json").write_text(encoded({"directory":str(patched[2])})+"\n",encoding="utf-8")
        print(encoded({"status":"trial_candidate","source_sha256":patched[1]["source_sha256"],
                       "parent_source_sha256":manifest["source_sha256"],"artifact":str(patched[2]),
                       "validation":patched[1]["validation"],"budget":budget.report()["gap_requests"]}))
        return 0
    except Exception as exc:
        (root/"revision-failure.json").write_text(encoded({"error_type":type(exc).__name__,"error":str(exc)[:1000]})+"\n",encoding="utf-8")
        raise
    finally:await client.aclose()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial",type=Path,required=True)
    parser.add_argument("--grant-authorization",help="exact user authorization record for one additional request")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
