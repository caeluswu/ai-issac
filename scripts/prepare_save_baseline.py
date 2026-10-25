"""Freeze an owner-supplied unlocked save from a stopped-game backup directory.

The caller asserts that the slot is fully unlocked; this script verifies file
integrity and records that declaration, not the achievement contents.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True, help="stable backup, never the live save directory")
    parser.add_argument("--slot", type=int, choices=(1, 2, 3), default=1)
    args = parser.parse_args()
    destination = ROOT/"artifacts/save-baselines"/f"slot-{args.slot}"
    manifest = ROOT/"configs/task"/f"save-baseline.slot-{args.slot}.json"
    if destination.exists() or manifest.exists(): raise FileExistsError("Baseline already frozen; never overwrite it")
    primary = f"persistentgamedata{args.slot}.dat"
    if not (args.source_dir/primary).is_file(): raise FileNotFoundError(args.source_dir/primary)
    destination.mkdir(parents=True)
    files = {}
    for name in (primary, f"gamestate{args.slot}.dat"):
        source = args.source_dir/name
        if source.is_file():
            target = destination/name
            shutil.copyfile(source, target)
            files[name] = {"path": str(target.relative_to(ROOT)).replace("\\", "/"),
                           "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
            target.chmod(0o444)
    value = {"schema_version": 1, "slot": args.slot, "unlock_status": "owner_declared_fully_unlocked",
             "unlock_verification": "project owner confirmation; no automated achievement verification",
             "created_at": datetime.now(timezone.utc).isoformat(), "files": files}
    manifest.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    print(manifest)


if __name__ == "__main__": main()
