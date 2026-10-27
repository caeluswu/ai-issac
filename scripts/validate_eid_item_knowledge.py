"""Offline integration check: actual pinned EID -> per-item SQLite -> AI context.

Starts fresh Python query processes. Does not launch the game or call any model.
Only validation metadata, never source text, enters the report.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"SocketBridge/python"))
from combat.item_knowledge import KnowledgeGateway
from mechanisms.developer import skill_request


def query(*args, expected=0):
    result=subprocess.run([sys.executable,str(ROOT/"query_item.py"),*args],cwd=ROOT,
                          capture_output=True,encoding="utf-8",timeout=30)
    if result.returncode!=expected:raise AssertionError(result.stderr or result.stdout)
    return json.loads(result.stdout)


def validate():
    gateway=KnowledgeGateway.from_config(ROOT)
    package=gateway.package
    before=hashlib.sha256(package.database.read_bytes()).hexdigest()
    initial=query("--list")["items"]
    first=query("69","--purpose","component_development")
    second=query("69","--purpose","planning")
    record=first["results"][0]["records"][0]
    assert record["summary"]["text"]
    assert record["summary"]["text"]==second["results"][0]["records"][0]["summary"]["text"]
    assert second["local_knowledge"]["cache_hits"]==1
    assert record["source_evidence"][0]["rights"]["ai_input"]=="unknown"
    assert record["claims"][0]["evidence"]["review_state"]=="unreviewed"
    missing=query("999999",expected=1)
    assert missing["status"]=="unknown" and missing["local_knowledge"]["saved_records"]==0
    final=query("--list")["items"]
    matching=[r for r in final if r["kind"]=="collectible" and r["id"]==69 and r["requested_language"]=="zh-CN"]
    assert len(matching)==1
    assert not any(r["id"]==999999 for r in final)
    calibration=json.loads((ROOT/"configs/mechanisms/chocolate-milk-j835.json").read_text(encoding="utf-8"))
    task={"task_id":"offline-eid-context-validation","dataset_id":package.dataset_id,
          "subject":{"variant":100,"id":69},"observed_inventory":{"collectibles":{},"trinkets":[]},
          "observed_stats":{k:calibration["scope"][k] for k in ("player_type","weapon_type","damage","max_fire_delay","shot_speed")}}
    context=skill_request(ROOT,task)
    assert context==skill_request(ROOT,task)
    assert context["sources"]["eid"]["results"][0]["records"][0]["summary"]["text"]==record["summary"]["text"]
    after=hashlib.sha256(package.database.read_bytes()).hexdigest()
    assert before==after
    report={"status":"passed","dataset_id":package.dataset_id,"source_database_sha256":after,
        "source_database_unchanged":True,"item":{"kind":"collectible","id":69,"requested_language":"zh-CN",
        "source_language":record["summary"]["language"],"text_characters":len(record["summary"]["text"]),
        "text_sha256":hashlib.sha256(record["summary"]["text"].encode()).hexdigest(),
        "parser_complete":record["parser_complete"],"source_revision":record["source_evidence"][0]["revision"]},
        "initial_cached_items":len(initial),"final_cached_items":len(final),"matching_item_rows":len(matching),
        "first_query":first["local_knowledge"],"fresh_process_query":second["local_knowledge"],
        "unknown_item_creates_no_record":True,"development_context_contains_eid_text":True,
        "development_context_stable":True,"model_http_requests":0,"game_launched":False,
        "local_database":"artifacts/knowledge/queried-items.sqlite"}
    destination=ROOT/"artifacts/validation/eid-item-knowledge-validation.json"
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=True,indent=2))


if __name__=="__main__":validate()
