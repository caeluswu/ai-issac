"""From SocketBridge/python: python -m apps.first_room [--local-only]."""
import argparse
import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from facade import SocketBridge
from combat.planner import load_env, DeepSeekClient, Planner, KnowledgeStore
from combat.runtime import CombatSession, NativePause, RunLog

ROOT = Path(__file__).resolve().parents[3]


async def main(args):
    load_env(ROOT/".env.local")
    client = None if args.local_only else DeepSeekClient()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    log = RunLog(ROOT/"artifacts/runs"/run_id)
    log.manifest(ROOT, client.model if client else None)
    planner = Planner(client, KnowledgeStore(ROOT/"configs/knowledge"), log) if client else None
    session = CombatSession(SocketBridge(port=args.port), planner,
                            NativePause(game_executable=ROOT/"The Binding of Isaac Rebirth Repentance"/"isaac-ng.exe"),
                            log, ready_timeout=args.ready_timeout,
                            new_run=args.new_run, seed=args.seed)
    print(f"Waiting for a fresh Isaac Normal run; the game window may stay in the background. Trace: {log.directory}", flush=True)
    report = await session.run()
    print(report.model_dump_json(indent=2), flush=True)
    return 0 if report.success and (args.local_only or report.llm_configured) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-only", action="store_true", help="diagnostic run without DeepSeek; not full phase acceptance")
    parser.add_argument("--port", type=int, default=9527)
    parser.add_argument("--ready-timeout", type=float, default=120)
    parser.add_argument("--new-run", action="store_true", help="before measurement, restart an existing run as Isaac")
    parser.add_argument("--seed", help="before measurement, start a recorded seed; requires an existing running game")
    args = parser.parse_args()
    if args.seed and not re.fullmatch(r"[A-Z0-9]{4} [A-Z0-9]{4}", args.seed): parser.error("seed format is XXXX XXXX")
    raise SystemExit(asyncio.run(main(args)))
