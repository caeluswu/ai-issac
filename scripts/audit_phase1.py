"""Independently inspect raw phase-one traces, without importing the controller.

Usage: python scripts/audit_phase1.py artifacts/runs/batch-... --output PATH
The audit checks recorded effects and reports; it cannot verify save unlocks or
claim absence of game modifications beyond the recorded source and game hashes.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024*1024), b""): h.update(block)
    return h.hexdigest()


def rows(path):
    with path.open(encoding="utf-8") as source:
        for line in source: yield json.loads(line)


def snapshot(row):
    return (row.get("message", {}).get("payload", {}).get("COMBAT_FRAME")
            if row["kind"] == "game_message" else None)


def audit_run(directory):
    trace = directory/"trace.jsonl"
    report = json.loads((directory/"report.json").read_text(encoding="utf-8"))
    last = None
    for row in rows(trace): last = snapshot(row) or last
    if last is None: return {"directory": str(directory), "issues": ["NO_SNAPSHOT"], "report": report}
    target = last["room_visit_id"]
    previous = None
    loss = 0
    clears = {}
    counts = Counter()
    issues = []
    sequence = 0
    measured = False
    start = None
    rooms = set()
    installed = False
    last_action = 0
    pause = None
    safe_clear = None
    for row in rows(trace):
        counts[row["kind"]] += 1
        if row["sequence"] != sequence+1: issues.append("TRACE_SEQUENCE_GAP")
        sequence = row["sequence"]
        if row["kind"] == "run_start": measured = True; start = row
        if measured and row["kind"] == "initialization_command": issues.append("INITIALIZATION_DURING_TASK")
        if row["kind"] == "pause_confirmed": pause = row
        if row["kind"] == "safe_clear_confirmed": safe_clear = row
        if row["kind"] == "plan_installed" and row["source"] == "deepseek": installed = True
        if row["kind"] == "action_sent":
            if row["action_id"] <= last_action: issues.append("NONMONOTONIC_ACTION_ID")
            last_action = row["action_id"]
        s = snapshot(row)
        if s is None: continue
        if measured: rooms.add(s["room_visit_id"])
        if measured and (s["room_visit_id"] == target or report.get("damage_scope") == "task"):
            hp = s["health"]["red_hearts"]+s["health"]["soul_hearts"]
            if previous:
                old = previous["health"]["red_hearts"]+previous["health"]["soul_hearts"]
                loss += max(0, old-hp)
            blockers = [e for e in s["enemies"] if e.get("can_shut_doors", True)
                        and not e["dead"] and not e["friendly"] and e["hp"] > 0]
            if (s["room_visit_id"] == target and not s["paused"] and s["room"]["is_clear"] and s["room"]["enemy_count"] == 0
                    and not blockers and not s["player"]["dead"] and hp > 0):
                clears[s["frame"]] = {"frame": s["frame"], "room_visit_id": target,
                    "is_clear": True, "enemy_count": 0, "blocking_hostiles": 0, "hp": hp}
            elif s["room_visit_id"] == target and not s["paused"]:
                clears.clear()
        previous = s
    if report["damage_half_hearts"] != loss: issues.append("DAMAGE_REPORT_MISMATCH")
    if report["llm_configured"] != installed: issues.append("LLM_REPORT_MISMATCH")
    if report["success"] and (len(clears) < 3 or len(rooms) < 2 or not start or pause is None):
        issues.append("SUCCESS_WITHOUT_REQUIRED_EFFECT_EVIDENCE")
    if report["success"] and "safe_clear_ticks" in report:
        if not safe_clear or safe_clear["frame"]-safe_clear["safe_since"] < 15 or len(clears) < 18:
            issues.append("MISSING_SAFE_CLEAR_SETTLING")
    if start and report["seed"] != start["seed"]: issues.append("SEED_REPORT_MISMATCH")
    if report["success"] and (last["stats"]["player_type"] != 0 or last["room"]["difficulty"] != 0):
        issues.append("WRONG_CHARACTER_OR_DIFFICULTY")
    return {"directory": str(directory), "report": report, "issues": sorted(set(issues)),
            "trace_sha256": digest(trace), "report_sha256": digest(directory/"report.json"),
            "manifest_sha256": digest(directory/"manifest.json"), "audited_damage_half_hearts": loss,
            "observed_room_visits": len(rooms), "clear_evidence": list(clears.values())[-3:],
            "initial_deepseek_plan_installed": installed, "pause_evidence": pause,
            "event_counts": dict(counts)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    batch = json.loads((args.batch/"summary.json").read_text(encoding="utf-8"))
    seeds = json.loads((args.batch/"seeds.json").read_text(encoding="utf-8"))
    seed_hash = hashlib.sha256("\n".join(seeds["seeds"]).encode()).hexdigest()
    if seed_hash != seeds["seeds_sha256"]: raise ValueError("SEED_MANIFEST_HASH_MISMATCH")
    results = [audit_run(args.batch/f"sample-{i:02d}") for i in range(1, len(seeds["seeds"])+1)]
    for seed, result in zip(seeds["seeds"], results):
        if seed != result["report"]["seed"]: result["issues"].append("PRECOMMITTED_SEED_MISMATCH")
    report = {"batch": str(args.batch.resolve()), "seed_manifest": seeds, "runs": results,
        "completed": batch["completed"], "physical_clears": sum(r["report"]["success"] for r in results),
        "full_llm_clears": sum(r["report"]["success"] and r["initial_deepseek_plan_installed"] for r in results),
        "mean_loss_half_hearts": statistics.mean(r["audited_damage_half_hearts"] for r in results),
        "mean_combat_seconds": statistics.mean(r["report"]["combat_ticks"]/30 for r in results),
        "issues": sum(len(r["issues"]) for r in results)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in ("runs", "seed_manifest")}, indent=2))
    return 1 if report["issues"] else 0


if __name__ == "__main__": raise SystemExit(main())
