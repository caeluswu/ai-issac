"""Freeze random seeds before any combat, then run every sample without selection."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent


def checked_seeds(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    digest = hashlib.sha256("\n".join(value["seeds"]).encode()).hexdigest()
    if digest != value["seeds_sha256"]: raise ValueError("SEED_MANIFEST_HASH_MISMATCH")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--slot", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--seeds", type=Path, help="replay an existing frozen list instead of generating one")
    parser.add_argument("--local-only", action="store_true", help="paired rule baseline")
    parser.add_argument("--stop-after-failures", type=int, help="stop unstarted samples once this failure budget is reached")
    parser.add_argument("--stop-file", type=Path, help="finish the active sample, then stop when this file exists")
    args = parser.parse_args()
    if not 1 <= args.count <= 100: parser.error("count must be 1..100")
    if args.stop_after_failures is not None and args.stop_after_failures < 1: parser.error("failure budget must be positive")
    destination = ROOT/"artifacts/runs"/("batch-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    destination.mkdir(parents=True)
    base = [sys.executable, str(ROOT/"run_first_room.py"), "--slot", str(args.slot)]
    seed_file = destination/"seeds.json"
    if args.seeds:
        manifest = checked_seeds(args.seeds)
        seed_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    else:
        print(f"Preparing seed batch: {destination}", flush=True)
        result = subprocess.run(base+["--startup-only", "--prepare-seeds", str(seed_file),
                                "--seed-count", str(args.count), "--output-dir", str(destination/"setup")], cwd=ROOT)
        if result.returncode: return result.returncode
        manifest = checked_seeds(seed_file)
    reports = []
    stop_reason = None
    def save():
        (destination/"summary.json").write_text(json.dumps({"seed_manifest": manifest,
            "local_only": args.local_only, "completed": len(reports), "planned": len(manifest["seeds"]),
            "status": "stopped" if stop_reason else "complete" if len(reports) == len(manifest["seeds"]) else "running",
            "stop_reason": stop_reason, "unstarted": len(manifest["seeds"])-len(reports),
            "reports": reports}, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    for index, seed in enumerate(manifest["seeds"], 1):
        if args.stop_file and args.stop_file.exists():
            stop_reason = "ADMINISTRATIVE_STOP_FILE"
            save()
            break
        run_dir = destination/f"sample-{index:02d}"
        print(f"[{index}/{len(manifest['seeds'])}] {seed}", flush=True)
        with (destination/f"sample-{index:02d}-console.txt").open("w", encoding="utf-8") as output:
            cmd = base+["--seed", seed, "--output-dir", str(run_dir)]
            if args.local_only: cmd.append("--local-only")
            began = time.monotonic()
            try:
                result = subprocess.run(cmd, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, timeout=300)
                exit_code = result.returncode
            except subprocess.TimeoutExpired:
                exit_code = 124
            process_wall = time.monotonic()-began
        report = json.loads((run_dir/"report.json").read_text(encoding="utf-8")) if (run_dir/"report.json").exists() else {
            "result": "PROCESS_FAILED", "success": False, "error": "No report; inspect child console"}
        if exit_code == 124: report = {**report, "result": "PROCESS_TIMEOUT", "success": False, "error": "Child process exceeded 300 seconds"}
        reports.append({"index": index, "planned_seed": seed, "exit_code": exit_code,
                        "run_directory": str(run_dir), "process_wall_seconds": process_wall, **report})
        failures = sum(not r["success"] or r["exit_code"] != 0 for r in reports)
        if args.stop_after_failures and failures >= args.stop_after_failures and index < len(manifest["seeds"]):
            stop_reason = "FAILURE_BUDGET_REACHED"
        save()
        print(f"  {report['result']}; damage={report.get('damage_half_hearts')}; error={report.get('error')}", flush=True)
        if stop_reason: break
    print(f"Batch artifacts: {destination}", flush=True)
    return 0 if not stop_reason and all(r["exit_code"] == 0 for r in reports) else 1


if __name__ == "__main__": raise SystemExit(main())
