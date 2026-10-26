"""Package compact text evidence; keep saves, credentials and raw runs local."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import statistics

ROOT = Path(__file__).resolve().parents[1]
EVENTS = {"run_start", "pause_confirmed", "resume_confirmed", "plan_installed", "safe_clear_confirmed",
          "boundary", "contract_override", "environment_entered", "environment_resolved", "environment_replan_started",
          "environment_diagnosis", "environment_diagnosis_failed", "replan_started", "replan_rejected", "fallback", "fault", "cleanup_error", "report"}


def metrics(label, summary):
    reports = summary["reports"]
    def mean(key, scale=1):
        values = [r[key]/scale for r in reports if r.get(key) is not None]
        return statistics.mean(values) if values else None
    return {"stage": label, "planned": summary["planned"], "completed": len(reports),
            "clears": sum(r["success"] for r in reports),
            "no_hit_clears": sum(r["success"] and r.get("damage_half_hearts") == 0 for r in reports),
            "mean_loss_half_hearts": mean("damage_half_hearts"),
            "max_loss_half_hearts": max((r.get("damage_half_hearts", 0) for r in reports), default=None),
            "mean_combat_seconds": mean("combat_ticks", 30), "mean_task_game_seconds": mean("task_ticks", 30),
            "successful_mean_combat_seconds": statistics.mean(r["combat_ticks"]/30 for r in reports if r["success"]) if any(r["success"] for r in reports) else None,
            "mean_pause_seconds": mean("pause_seconds"), "mean_process_wall_seconds": mean("process_wall_seconds"),
            "max_decision_p95_ms": max((r.get("decision_p95_ms") or 0 for r in reports), default=None),
            "max_decision_p99_ms": max((r.get("decision_p99_ms") or 0 for r in reports), default=None),
            "max_navigation_p95_ms": max((r.get("navigation_decision_p95_ms") or 0 for r in reports), default=None),
            "max_navigation_p99_ms": max((r.get("navigation_decision_p99_ms") or 0 for r in reports), default=None),
            "contract_override_entries": sum(r.get("contract_override_count", 0) for r in reports),
            "replan_requests": sum(r.get("replan_requests", 0) for r in reports),
            "stall_or_timeout_samples": [r["index"] for r in reports if any(
                word in (r["result"]+" "+(r.get("error") or "")) for word in ("STALL", "TIMEOUT"))],
            "latency_violation_samples": [r["index"] for r in reports if any((r.get(key) or 0) > limit for key, limit in (
                ("decision_p95_ms",20),("decision_p99_ms",30),("navigation_decision_p95_ms",20),("navigation_decision_p99_ms",30)))],
            "damage_scope": sorted({r.get("damage_scope", "target_room") for r in reports})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("qualification", type=Path)
    args = parser.parse_args()
    qualification = json.loads((args.qualification/"qualification.json").read_text(encoding="utf8"))
    destination = args.qualification/"batches"
    destination.mkdir(exist_ok=True)
    index = []
    stages = []
    for stage in qualification["stages"]:
        source = Path(stage["directory"])
        label = (stage.get("stage") or ("holdout" if stage.get("holdout") else "regression"))+"-"+stage.get("role", "llm")
        target = destination/label
        target.mkdir(exist_ok=True)
        for name in ("summary.json", "seeds.json"): shutil.copyfile(source/name, target/name)
        summary = json.loads((source/"summary.json").read_text(encoding="utf8"))
        stages.append(metrics(label, summary))
        for row in summary["reports"]:
            run, package = source/f"sample-{row['index']:02d}", target/f"sample-{row['index']:02d}"
            package.mkdir(exist_ok=True)
            for name in ("report.json", "manifest.json", "startup.json", "game-log.txt"):
                if (run/name).is_file(): shutil.copyfile(run/name, package/name)
            digest = hashlib.sha256()
            events = []
            measured = False
            enemies, shapes, rooms, objectives = set(), set(), set(), set()
            intended_room, entries = None, []
            max_hostiles = max_projectiles = 0
            trace = run/"trace.jsonl"
            if trace.exists():
                with trace.open("rb") as stream:
                    for line in stream:
                        digest.update(line)
                        event = json.loads(line)
                        if event["kind"] in EVENTS: events.append(event)
                        if event["kind"] == "run_start": measured = True
                        if event["kind"] == "door_selected": intended_room = event["target_room"]
                        message = event.get("message", {})
                        if measured and message.get("event") == "ROOM_ENTER" and intended_room is not None:
                            actual = message["data"]["room_index"]
                            entries.append({"intended_room": intended_room, "actual_room": actual, "redirected": intended_room != actual})
                            intended_room = None
                        state = event.get("message", {}).get("payload", {}).get("COMBAT_FRAME")
                        if measured and state:
                            rooms.add(state["room_visit_id"])
                            if state["room"].get("room_shape") is not None: shapes.add(state["room"]["room_shape"])
                            hostiles = [e for e in state["enemies"] if not e["friendly"] and not e["dead"] and e["hp"] > 0]
                            enemies.update((e["type"], e["variant"], e.get("champion_color", -1)) for e in hostiles)
                            max_hostiles = max(max_hostiles, len(hostiles))
                            max_projectiles = max(max_projectiles, len(state["projectiles"]["enemy_projectiles"]))
                        objective = event.get("metadata", {}).get("diagnostics", {}).get("environment_objective")
                        if objective: objectives.add(objective)
            (package/"events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf8")
            index.append({"stage": label, "sample": row["index"], "seed": row.get("seed"), "result": row["result"],
                          "raw_trace": str(trace.relative_to(ROOT)), "trace_sha256": digest.hexdigest() if trace.exists() else None,
                          "observed_enemy_type_variant_champions": sorted(enemies), "observed_room_shapes": sorted(shapes),
                          "observed_room_visits": len(rooms), "max_hostiles": max_hostiles, "max_enemy_projectiles": max_projectiles,
                          "environment_objectives": sorted(objectives), "door_entries": entries})
    (args.qualification/"evidence-index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf8")
    (args.qualification/"metrics.json").write_text(json.dumps(stages, ensure_ascii=False, indent=2), encoding="utf8")
    print(f"Packaged {len(index)} run records under {destination}")


if __name__ == "__main__": main()
