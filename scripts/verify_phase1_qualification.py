"""Supplemental effect/source/baseline checks; no policy or collision simulation."""
import argparse
import json
from pathlib import Path


def verify_run(directory, frozen):
    report = json.loads((directory/"report.json").read_text(encoding="utf-8"))
    manifest = json.loads((directory/"manifest.json").read_text(encoding="utf-8"))
    issues = []
    if any(frozen.get(path) != digest for path, digest in manifest["source_hashes"].items()): issues.append("SOURCE_NOT_FROZEN")
    measured, last, goal_sequence, manual_sequence = False, None, None, None
    blocking_rooms = set()
    contract_entries = 0
    with (directory/"trace.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line); message = row.get("message", {})
            if row["kind"] == "run_start": measured = True
            if row["kind"] == "safe_clear_confirmed": goal_sequence = row["sequence"]
            if row["kind"] == "contract_override": contract_entries += 1
            if message.get("type") == "CMD" and message.get("result", {}).get("success") and message["result"].get("mode") == "MANUAL": manual_sequence = row["sequence"]
            state = message.get("payload", {}).get("COMBAT_FRAME")
            if state:
                last = state
                if measured and state["room"]["enemy_count"] > 0 and any(
                    e.get("can_shut_doors", True) and not e["dead"] and not e["friendly"] and e["hp"] > 0
                    for e in state["enemies"]): blocking_rooms.add(state["room_visit_id"])
    if report.get("contract_override_count", 0) != contract_entries: issues.append("CONTRACT_COUNT_MISMATCH")
    if report["success"]:
        if not last or last["room_visit_id"] not in blocking_rooms: issues.append("NO_BLOCKING_COMBAT_OBSERVED")
        if goal_sequence is None or manual_sequence is None or manual_sequence <= goal_sequence: issues.append("MANUAL_NOT_CONFIRMED_AFTER_CLEAR")
        if last and (last["player"]["dead"] or last["health"]["red_hearts"]+last["health"]["soul_hearts"] <= 0): issues.append("DEAD_AT_COMPLETION")
    startup = json.loads((directory/"startup.json").read_text(encoding="utf-8"))
    prepared = next((r for r in startup if r["phase"] == "prepared"), {})
    if prepared.get("installed_mod_sha256") != manifest["source_hashes"].get("SocketBridge\\main.lua"): issues.append("INSTALLED_MOD_MISMATCH")
    baseline = prepared.get("restored_baseline")
    if not baseline: issues.append("NO_RESTORED_BASELINE")
    return {"directory": str(directory), "result": report["result"], "success": report["success"], "issues": issues,
            "combat_observed": bool(last and last["room_visit_id"] in blocking_rooms), "manual_ack_sequence": manual_sequence,
            "safe_clear_sequence": goal_sequence, "contract_entries": contract_entries, "baseline": baseline,
            "dependencies": manifest.get("dependencies", {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("qualification", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = json.loads((args.qualification/"qualification.json").read_text(encoding="utf-8"))
    runs = []
    for stage in value["stages"]:
        directory = Path(stage["directory"])
        summary = json.loads((directory/"summary.json").read_text(encoding="utf-8"))
        for row in summary["reports"]: runs.append(verify_run(directory/f"sample-{row['index']:02d}", value["source_hashes"]))
    baselines = {json.dumps(r["baseline"], sort_keys=True) for r in runs}
    dependencies = {json.dumps(r["dependencies"], sort_keys=True) for r in runs}
    issues = sum(len(r["issues"]) for r in runs)
    if len(baselines) != 1: issues += 1
    if len(dependencies) != 1: issues += 1
    report = {"qualification": str(args.qualification), "runs": runs, "checked": len(runs),
              "same_baseline": len(baselines) == 1, "same_dependencies": len(dependencies) == 1,
              "issues": issues, "passed": not issues}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "runs"}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
