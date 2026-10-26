"""Launch the game, prepare an Isaac Normal run, and execute phase one."""
import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import re
import hashlib
import json
import secrets
from facade import SocketBridge
from combat.launcher import GameLauncher
from combat.planner import DeepSeekClient, KnowledgeStore, Planner, load_env
from combat.runtime import CombatSession, NativePause, RunLog

ROOT = Path(__file__).resolve().parents[3]


async def main(args):
    load_env(ROOT/".env.local", override=True)
    client = None if args.local_only or args.startup_only else DeepSeekClient()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    log = RunLog(args.output_dir or ROOT/"artifacts/runs"/run_id)
    log.manifest(ROOT, client.model if client else None)
    planner = Planner(client, KnowledgeStore(ROOT/"configs/knowledge"), log) if client else None
    learning = None
    if getattr(args,"with_items",False):
        from combat.item_knowledge import KnowledgeGateway
        from combat.item_learning import ItemLearning
        learning = ItemLearning(KnowledgeGateway.from_config(ROOT))
        log("knowledge_pinned",dataset_id=learning.gateway.package.dataset_id,active_experiments=False)
    pause = NativePause(game_executable=ROOT/"The Binding of Isaac Rebirth Repentance/isaac-ng.exe")
    launcher = GameLauncher(ROOT, log, pause, slot=args.slot, timeout=args.ready_timeout,
                            require_baseline=not args.local_only and not args.startup_only)
    session = CombatSession(SocketBridge(), planner, pause, log,
                            ready_timeout=args.ready_timeout, new_run=True, seed=args.seed,learning=learning)
    print(f"Run artifacts: {log.directory}", flush=True)
    async def freeze_seeds(current):
        if not args.prepare_seeds: return
        values = []
        while len(values) < args.seed_count:
            number = secrets.randbelow(2**32-1)+1
            if number not in values: values.append(number)
        response = await current.rpc("FORMAT_SEEDS", {"values": values})
        seeds = response["seeds"]
        if len(seeds) != len(values) or any(not re.fullmatch(r"[A-Z0-9]{4} [A-Z0-9]{4}", s) for s in seeds):
            raise RuntimeError("INVALID_FORMATTED_SEEDS")
        manifest = {"created_at": datetime.now(timezone.utc).isoformat(),
                    "method": "OS CSPRNG distinct uniform nonzero uint32; game Seeds.Seed2String",
                    "values": values, "seeds": seeds,
                    "seeds_sha256": hashlib.sha256("\n".join(seeds).encode()).hexdigest()}
        with args.prepare_seeds.open("x", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        log("seeds_frozen", **manifest)
    try:
        report = await session.run(bootstrap=launcher, startup_only=args.startup_only, on_ready=freeze_seeds)
    finally:
        launcher.collect_evidence()
        if client: await client.aclose()
    print(report.model_dump_json(indent=2), flush=True)
    if args.startup_only: return 0 if report.result == "STARTUP_READY" else 1
    return 0 if report.success and (args.local_only or report.llm_configured) else 1


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--ready-timeout", type=float, default=75)
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--startup-only", action="store_true", help="verify startup, snapshot and native pause; no combat")
    parser.add_argument("--with-items",action="store_true",help="enable pinned EID decisions, bounded pickups and passive experience")
    parser.add_argument("--seed", help="debug replay seed, XXXX XXXX; omitted means a new random run")
    parser.add_argument("--output-dir", type=Path, help="new artifact directory; existing directories are never overwritten")
    parser.add_argument("--prepare-seeds", type=Path, help="write a new precommitted seed list; requires --startup-only")
    parser.add_argument("--seed-count", type=int, default=10)
    args = parser.parse_args()
    if args.seed and not re.fullmatch(r"[A-Z0-9]{4} [A-Z0-9]{4}", args.seed): parser.error("seed format is XXXX XXXX")
    if args.ready_timeout <= 0: parser.error("ready-timeout must be positive")
    if not 1 <= args.seed_count <= 100: parser.error("seed-count must be 1..100")
    if args.prepare_seeds and not args.startup_only: parser.error("prepare-seeds requires startup-only")
    if args.prepare_seeds and args.prepare_seeds.exists(): parser.error("seed manifest already exists")
    return asyncio.run(main(args))


if __name__ == "__main__": raise SystemExit(cli())
