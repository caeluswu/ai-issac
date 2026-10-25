"""Strict regression/holdout gate; raw failures and audits stay in denominators."""
import argparse
import json
from pathlib import Path
import statistics
import hashlib
from audit_phase1 import audit_run


def assess(directory, role, holdout=False):
    summary = json.loads((directory/"summary.json").read_text(encoding="utf-8"))
    reports, issues, audits = summary["reports"], [], []
    seeds = json.loads((directory/"seeds.json").read_text(encoding="utf-8"))
    if hashlib.sha256("\n".join(seeds["seeds"]).encode()).hexdigest() != seeds["seeds_sha256"]: issues.append("SEED_HASH_MISMATCH")
    hashes = set()
    if summary["completed"] != summary["planned"]: issues.append("INCOMPLETE_BATCH")
    if summary["local_only"] != (role == "rule"): issues.append("WRONG_SELECTOR_ROLE")
    if not holdout and summary["planned"] != 10: issues.append("REGRESSION_SIZE_GATE")
    for row in reports:
        run = directory/f"sample-{row['index']:02d}"
        if (run/"trace.jsonl").exists() and (run/"report.json").exists():
            audit = audit_run(run); audits.append(audit)
            if any(row.get(key) != value for key, value in audit["report"].items()):
                issues.append(f"{row['index']}:SUMMARY_REPORT_MISMATCH")
            manifest = json.loads((run/"manifest.json").read_text(encoding="utf-8"))
            hashes.add(json.dumps(manifest["source_hashes"], sort_keys=True))
            issues.extend(f"{row['index']}:{issue}" for issue in audit["issues"])
        else: issues.append(f"{row['index']}:MISSING_RAW_EVIDENCE")
        if row.get("seed") != seeds["seeds"][row["index"]-1]: issues.append(f"{row['index']}:SEED_MISMATCH")
        if row["result"] in ("STALL_UNRECOVERED", "COMBAT_TIMEOUT", "PROCESS_TIMEOUT"): issues.append(f"{row['index']}:FORBIDDEN_STALL_OR_TIMEOUT")
        if role == "llm" and row["success"] and not row["llm_configured"]: issues.append(f"{row['index']}:NO_LLM_CONFIGURATION")
    wins = sum(r["success"] and r.get("exit_code") == 0 for r in reports)
    if len(hashes) != 1: issues.append("MIXED_IMPLEMENTATIONS")
    needed = 48 if holdout else len(reports)
    if wins < needed: issues.append("INSUFFICIENT_CLEARS")
    loss = statistics.mean(r.get("damage_half_hearts", 0) for r in reports) if reports else None
    times = [r.get("combat_ticks", 0)/30 for r in reports]
    if role == "llm" and loss is not None and loss > .4: issues.append("HEALTH_LOSS_GATE")
    if role == "llm" and not holdout and reports:
        if statistics.mean(times) > 15.85: issues.append("MEAN_COMBAT_TIME_GATE")
        if max(times) > 45: issues.append("MAX_COMBAT_TIME_GATE")
        if sum(r["success"] and r.get("damage_half_hearts") == 0 for r in reports) < 8: issues.append("NO_DAMAGE_GATE")
    if any((r.get("decision_p95_ms") or 0) > 20 or (r.get("decision_p99_ms") or 0) > 30 for r in reports): issues.append("DECISION_LATENCY_GATE")
    if holdout and summary["planned"] != 50: issues.append("HOLDOUT_SIZE_GATE")
    return {"directory": str(directory), "role": role, "holdout": holdout, "planned": summary["planned"],
        "clears": wins, "mean_loss_half_hearts": loss, "mean_combat_seconds": statistics.mean(times) if times else None,
        "max_combat_seconds": max(times) if times else None, "issues": issues, "passed": not issues, "audits": audits}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    parser.add_argument("--role", choices=("llm", "rule"), required=True)
    parser.add_argument("--holdout", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = assess(args.batch, args.role, args.holdout)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "audits"}, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
