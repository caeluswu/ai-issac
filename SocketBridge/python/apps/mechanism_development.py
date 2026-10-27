"""Explicit development workflow; listing/recording/publishing never calls LLM."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import hashlib
from combat.agent_store import AgentStore
from combat.builds import digest
from combat.item_knowledge import KnowledgeGateway
from combat.planner import DeepSeekClient,load_env
from mechanisms.budget import DevelopmentBudget
from mechanisms.developer import MechanismDeveloper,skill_request
from mechanisms.store import SkillStore,read_candidate
from mechanisms.normalization import model_program
from mechanisms.program import canonical
from mechanisms.validation import validate_candidate

ROOT=Path(__file__).resolve().parents[3]


def agent_store():return AgentStore(ROOT/"artifacts/agent-state/agent.sqlite")


def record_probe(probe_id):
    if not re.fullmatch(r"charge-[0-9TZ]+",probe_id):raise ValueError("INVALID_PROBE_ID")
    path=ROOT/"artifacts/mechanism-probes"/probe_id
    summary=json.loads((path/"summary.json").read_text(encoding="utf-8"))
    if summary.get("success") is not True or not summary.get("stages"):raise ValueError("NEED_SUCCESSFUL_MECHANISM_MEASUREMENTS")
    observation=None
    with (path/"trace.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row=json.loads(line)
            if row.get("kind")=="probe_frame" and (row.get("inventory",{}).get("collectibles") or {}).get("69"):
                observation={k:row[k] for k in ("frame","room_visit_id","snapshot_id","stats","inventory")};break
    if not observation:raise ValueError("NO_OBSERVED_UNSUPPORTED_BUILD")
    gateway=KnowledgeGateway.from_config(ROOT)
    task={"dataset_id":gateway.package.dataset_id,"subject":{"variant":100,"id":69},"state":"pending",
          "trigger":"observed_unsupported_build","source_mode":"development_initialization","probe_id":probe_id,
          "context":observation,"reasons":["CAPABILITY_NOT_IMPLEMENTED"]}
    identity=digest(task)
    agent_store().record("task",identity,task)
    return {"task_id":identity,**task}


async def develop(identity,*,authorize_extra=False):
    if not re.fullmatch(r"[0-9a-f]{64}",identity):raise ValueError("INVALID_TASK_ID")
    store=agent_store();task=store.get("task",identity)
    if task is None:raise ValueError("UNKNOWN_TASK")
    if task.get("state")=="supported":return {"status":"already_supported","task":task}
    directory=ROOT/"artifacts/skill-development"/identity
    budget=DevelopmentBudget(directory/"budget.sqlite")
    if authorize_extra:budget.authorize_one_extra("explicit_human_authorization_for_one_transport_retry")
    if task.get("state") in ("validated_offline","requires_host_primitive") and (directory/"result.json").exists():
        result=json.loads((directory/"result.json").read_text(encoding="utf-8"))
        if result.get("candidate"):read_candidate(result["candidate"])
        return {**result,"cached":True}
    if budget.report()["requests"]>=budget.report()["max_requests"]:return {"status":"budget_exhausted","budget":budget.report()}
    request=skill_request(ROOT,{**task,"task_id":identity})
    load_env(ROOT/".env.local",override=True)
    client=DeepSeekClient(timeout=120,budget=budget,max_tokens=4096,retries=1,thinking="disabled")
    try:result=await MechanismDeveloper(client,budget,directory).develop(request)
    finally:await client.aclose()
    if result["status"] in ("validated_offline","rejected","requires_host_primitive"):
        store.resolve_task(identity,result["status"],result.get("candidate") or directory)
    return result


def recover_response(identity,response_id):
    if not re.fullmatch(r"[0-9a-f]{64}",identity) or not re.fullmatch(r"[0-9a-f]{32}",response_id):raise ValueError("INVALID_ARTIFACT_ID")
    directory=ROOT/"artifacts/skill-development"/identity
    request=json.loads((directory/"skill-request.json").read_text(encoding="utf-8"))
    attempt=directory/"requests"/response_id
    raw=(attempt/"response.json").read_bytes()
    usage=json.loads((attempt/"usage.json").read_text(encoding="utf-8"))
    program,normalization=model_program(raw,allow_recovery=True)
    if normalization["kind"]=="identity":raise ValueError("RESPONSE_DOES_NOT_REQUIRE_SYNTAX_RECOVERY")
    if not set(program.document["source_refs"]).issubset(request["allowed_source_refs"]):raise ValueError("UNBOUND_SOURCE_REFERENCE")
    calibration=request["sources"]["local"]["calibration"]
    validation=validate_candidate(program,calibration)
    if not validation["passed"]:raise ValueError("RECOVERED_PROGRAM_FAILED_INDEPENDENT_CHECKS: "+json.dumps(validation["errors"]))
    candidate=directory/"candidates"/program.sha256;candidate.mkdir(parents=True,exist_ok=False)
    provenance={"author":"deepseek","author_scope":"model_logic_host_syntax_recovery","normalization":normalization,
        "configured_model":None,"configured_model_source":"not_captured_before_initial_request",
        "response_model":usage.get("response_model"),"response_id":usage.get("response_id"),"task_id":identity,
        "response_sha256":hashlib.sha256(raw).hexdigest(),"program_sha256":program.sha256,"request_artifact":str(attempt),
        "task_request_sha256":hashlib.sha256(canonical(request)).hexdigest()}
    for name,value in (("program",program.document),("calibration",calibration),("provenance",provenance),("validation",validation)):
        (candidate/(name+".json")).write_bytes(canonical(value))
    previous=(directory/"result.json").read_bytes()
    (directory/"result.before-syntax-recovery.json").write_bytes(previous)
    result={"status":"validated_offline","candidate":str(candidate),"recovered_response":str(attempt),"normalization":normalization,
            "generation_api_calls":0,"budget":DevelopmentBudget(directory/"budget.sqlite").report()}
    (directory/"result.json").write_bytes(canonical(result))
    agent_store().resolve_task(identity,"validated_offline",candidate)
    return result


def cli():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest="command",required=True)
    commands.add_parser("list")
    record=commands.add_parser("record-probe");record.add_argument("probe_id")
    dev=commands.add_parser("develop");dev.add_argument("task_id")
    dev.add_argument("--authorize-one-extra-request",action="store_true",help="requires explicit human approval; preserves all prior attempts, adds at most one 4096-token request")
    publish=commands.add_parser("publish");publish.add_argument("candidate");publish.add_argument("--trial",required=True)
    quarantine=commands.add_parser("quarantine");quarantine.add_argument("program_sha256");quarantine.add_argument("--reason",required=True);quarantine.add_argument("--evidence",required=True)
    recover=commands.add_parser("recover-response");recover.add_argument("task_id");recover.add_argument("response_id")
    args=parser.parse_args()
    if args.command=="record-probe":result=record_probe(args.probe_id)
    elif args.command=="recover-response":result=recover_response(args.task_id,args.response_id)
    elif args.command=="list":
        with agent_store().connect() as db:rows=db.execute("SELECT id,body FROM learning_tasks ORDER BY id").fetchall()
        result=[{"task_id":identity,**json.loads(body)} for identity,body in rows if json.loads(body).get("subject",{}).get("id")==69]
    elif args.command=="publish":
        package=SkillStore(ROOT/"skills").publish(args.candidate,args.trial)
        provenance=read_candidate(args.candidate)[2]
        if provenance.get("task_id"):agent_store().resolve_task(provenance["task_id"],"supported",package)
        result={"package":str(package)}
    elif args.command=="quarantine":
        SkillStore(ROOT/"skills").quarantine(args.program_sha256,args.reason,args.evidence)
        result={"status":"quarantined","program_sha256":args.program_sha256}
    else:result=asyncio.run(develop(args.task_id,authorize_extra=args.authorize_one_extra_request))
    print(json.dumps(result,ensure_ascii=True,indent=2))
    return 1 if isinstance(result,dict) and result.get("status") in ("rejected","budget_exhausted","requires_host_primitive","interrupted") else 0
