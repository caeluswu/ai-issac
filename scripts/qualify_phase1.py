"""Run immutable regression gates before spending fresh development/holdout seeds."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from evaluate_phase1_optimization import assess

ROOT = Path(__file__).resolve().parents[1]


def source_hashes():
    paths = list((ROOT/"SocketBridge/python/combat").glob("*.py"))+list((ROOT/"SocketBridge/python/apps").glob("*.py"))
    paths += [ROOT/"SocketBridge/main.lua", ROOT/"SocketBridge/python/facade.py", ROOT/"SocketBridge/python/connection/server.py",
              ROOT/"run_first_room.py", ROOT/"run-first-room.ps1", ROOT/"run_phase1_batch.py"]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--through", choices=("regression", "development", "holdout"), default="holdout")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--development-seeds", type=Path, help="Replay a previously exposed development manifest; never label it held out")
    args = parser.parse_args()
    output = args.output_dir or ROOT/"artifacts/phase-1-optimization-20261026"/("qualification-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir(parents=True, exist_ok=False)
    frozen = source_hashes()
    result = {"implementation_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "source_hashes": frozen, "planned_through": args.through, "stages": [], "passed": False}
    def save(): (output/"qualification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    def run_batch(stage, role, seeds=None, count=None):
        if source_hashes() != frozen: raise RuntimeError("IMPLEMENTATION_CHANGED_DURING_QUALIFICATION")
        before = set((ROOT/"artifacts/runs").glob("batch-*"))
        command = [sys.executable, str(ROOT/"run_phase1_batch.py")]
        if seeds: command += ["--seeds", str(seeds)]
        else: command += ["--count", str(count)]
        if role == "rule": command += ["--local-only"]
        print(f"QUALIFICATION {stage}/{role}", flush=True)
        completed = subprocess.run(command, cwd=ROOT)
        created = set((ROOT/"artifacts/runs").glob("batch-*"))-before
        if len(created) != 1: raise RuntimeError("AMBIGUOUS_BATCH_DIRECTORY")
        directory = created.pop()
        if source_hashes() != frozen: raise RuntimeError("IMPLEMENTATION_CHANGED_DURING_QUALIFICATION")
        return directory, completed.returncode
    def gate(directory, role, stage):
        evidence = assess(directory, role, holdout=stage == "holdout")
        (output/f"{stage}-{role}-audit.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        return {k: v for k, v in evidence.items() if k != "audits"}
    try:
        seeds = ROOT/"artifacts/phase-1-validation-20261026/seeds.json"
        for role in ("rule", "llm"):
            directory, code = run_batch("regression", role, seeds=seeds)
            verdict = gate(directory, role, "regression")
            result["stages"].append(verdict); save()
            if not verdict["passed"]:
                print(json.dumps(verdict, ensure_ascii=False, indent=2), flush=True)
                return 1
        if args.through == "regression": result["passed"] = True; save(); return 0
        directory, code = run_batch("development", "llm", seeds=args.development_seeds, count=30)
        summary = json.loads((directory/"summary.json").read_text(encoding="utf-8"))
        failures = [r for r in summary["reports"] if not r["success"] or not r["llm_configured"]]
        result["stages"].append({"stage": "development", "directory": str(directory), "planned": 30,
                                 "clears": sum(r["success"] for r in summary["reports"]), "failures": failures,
                                 "passed": summary["completed"] == 30 and not failures})
        save()
        if failures or summary["completed"] != 30: return 1
        if args.through == "development": result["passed"] = True; save(); return 0
        directory, _ = run_batch("holdout", "llm", count=50)
        seeds = directory/"seeds.json"
        result["stages"].append(gate(directory, "llm", "holdout")); save()
        directory, _ = run_batch("holdout", "rule", seeds=seeds)
        result["stages"].append(gate(directory, "rule", "holdout")); save()
        result["passed"] = all(stage["passed"] for stage in result["stages"])
        save()
        return 0 if result["passed"] else 1
    except Exception as exc:
        result["error"] = type(exc).__name__+": "+str(exc); save()
        raise
    finally: print(f"Qualification evidence: {output}", flush=True)


if __name__ == "__main__": raise SystemExit(main())
