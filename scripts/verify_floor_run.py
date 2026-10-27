"""Replay a floor's raw evidence without game input or model calls."""
import argparse
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"SocketBridge/python"))
from campaign.judge import FloorJudge
from combat.models import WorldSnapshot


def verify(directory):
    directory=Path(directory)
    report=json.loads((directory/"floor-report.json").read_text(encoding="utf-8"))
    manifest=json.loads((directory/"manifest.json").read_text(encoding="utf-8"))
    judge=FloorJudge()
    frames=OrderedDict()
    last=None
    errors=[]
    manual=False
    installed_plans=[]
    requests=0
    for line in (directory/"trace.jsonl").open(encoding="utf-8"):
        row=json.loads(line)
        if row["kind"]=="game_message":
            raw=row["message"]
            value=raw.get("payload",{}).get("COMBAT_FRAME")
            if value:
                last=WorldSnapshot.model_validate(value)
                frames[last.room_visit_id,last.frame]=last
                if len(frames)>64:frames.popitem(last=False)
            judge.on_raw(raw)
            if raw.get("type")=="CMD" and raw.get("result",{}).get("mode") in ("FORCE_AI","MANUAL"):
                manual=raw["result"]["mode"]=="MANUAL" and raw["result"].get("success") is True
        elif row["kind"]=="run_start":
            if last is None or last.snapshot_id!=row["snapshot_id"]:raise ValueError("START_SNAPSHOT_NOT_FOUND")
            judge.begin(last)
        elif row["kind"]=="action_sent":
            if row["sent"] is not True:continue
            source=frames.get((row["room_visit_id"],row["frame"]))
            if source is None:raise ValueError("ACTION_SOURCE_SNAPSHOT_MISSING")
            judge.action(source,row["move"],row["shoot"],row["action_id"])
        elif row["kind"]=="floor_objective" and row["objective"]["kind"]=="leave_floor":
            judge.arm_exit(row["objective"]["target"]["instance_id"])
        elif row["kind"]=="initialization_command" and judge.start is not None:
            errors.append("INITIALIZATION_AFTER_TASK_START")
        elif row["kind"]=="plan_installed" and row["source"] in ("deepseek","deepseek_replan","deepseek_template"):
            installed_plans.append(row["source"])
        elif row["kind"]=="llm_request":requests+=1
    if manifest.get("normal_inputs_only") is not True:errors.append("NORMAL_INPUT_PROFILE_NOT_PROVEN")
    if not judge.success:errors.append("FLOOR_PROOF_INCOMPLETE")
    if not report["success"]:errors.append("RUN_REPORTED_FAILURE")
    if manifest.get("model") and (not report["llm_configured"] or not installed_plans):errors.append("LLM_PLAN_NOT_ADOPTED")
    if not manual or report["cleanup_errors"]:errors.append("MANUAL_CLEANUP_UNCONFIRMED")
    sources={path.replace("\\","/"):digest for path,digest in manifest["source_hashes"].items()}
    if any(not (ROOT/path).resolve().is_relative_to(ROOT.resolve()) for path in sources):
        raise ValueError("SOURCE_PATH_OUTSIDE_PROJECT")
    source_matches=all((ROOT/path).is_file() and hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest
        for path,digest in sources.items())
    judge_paths=("SocketBridge/python/campaign/judge.py","SocketBridge/python/campaign/models.py",
        "SocketBridge/python/combat/models.py","SocketBridge/python/combat/observation.py",
        "SocketBridge/python/combat/ledgers.py","SocketBridge/python/combat/boundary_safety.py")
    judge_matches=all(sources.get(path)==hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for path in judge_paths)
    if not judge_matches:errors.append("VERIFIER_JUDGE_VERSION_MISMATCH")
    return {"schema_version":1,"verifier_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),"passed":not errors,"errors":errors,"seed":report["seed"],
        "evaluation_mode":report.get("evaluation_mode","normal_random"),"source_hashes_match":source_matches,"judge_sources_match":judge_matches,
        "normal_input_scope":manifest["normal_inputs_only"],"manual_restored":manual,
        "independent_proof":judge.evidence(),"health_after":last.health if last else None,
        "damage_half_hearts":report["damage_half_hearts"],"game_ticks":report["game_ticks"],
        "process_wall_seconds":report.get("process_wall_seconds",report["wall_seconds"]),
        "pause_seconds":report["pause_seconds"],"llm_plan_sources":installed_plans,
        "planner_complete_calls":requests,"http_budget":report["request_budget"],
        "files_sha256":{name:hashlib.sha256((directory/name).read_bytes()).hexdigest()
            for name in ("manifest.json","trace.jsonl","floor-report.json","startup.json")}}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory",type=Path)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    result=verify(args.directory)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result["passed"] else 1)
