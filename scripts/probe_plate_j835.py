"""Diagnostic-only J835 Necropolis pressure plate observation or traversal."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "SocketBridge/python"))
from facade import SocketBridge
from combat.launcher import GameLauncher
from combat.runtime import CombatSession, NativePause, RunLog


async def main(observe_only=False, exit_room=False):
    directory = ROOT / "artifacts/runs" / datetime.now(timezone.utc).strftime("plate961-%Y%m%dT%H%M%S%fZ")
    log = RunLog(directory)
    log.manifest(ROOT, None)
    pause = NativePause(game_executable=ROOT / "The Binding of Isaac Rebirth Repentance/isaac-ng.exe")
    launcher = GameLauncher(ROOT, log, pause, slot=1, timeout=75)
    session = CombatSession(SocketBridge(), None, pause, log, ready_timeout=75,
                            new_run=True, seed="3BYH A82R")
    result = {"source_mode": "diagnostic_initialization", "formal_run_proof": False,
              "commands": [], "result": "INCOMPLETE"}
    print("Plate probe artifacts: " + str(directory), flush=True)
    try:
        snapshot = await session.open(bootstrap=launcher)
        for command, predicate in (
            ("stage 5a", lambda s: s.floor and s.floor.stage == 5 and s.floor.stage_type == 1),
            ("goto d.961", lambda s: s.room.get("room_variant") == 961 and s.room.get("room_type") == 1),
        ):
            await session.rpc("EXEC_CONSOLE", {"command": command})
            result["commands"].append(command)
            deadline = time.monotonic() + 20
            while not predicate(snapshot):
                if time.monotonic() > deadline:
                    raise TimeoutError("TRANSITION_TIMEOUT: " + command)
                snapshot = await session.next_snapshot(after=snapshot.snapshot_id, timeout=10)
        result["initial"] = {"frame": snapshot.frame, "stage": snapshot.floor.stage,
                             "stage_type": snapshot.floor.stage_type,
                             "room_variant": snapshot.room["room_variant"], "hp": snapshot.hp,
                             "pos": snapshot.player.pos.model_dump(),
                             "plates": [(i, c) for i, c in snapshot.layout["grid"].items() if c["type"] == 20]}
        if observe_only:
            result["samples"] = []
            began = snapshot.frame
            while snapshot.frame - began < 180 and snapshot.alive:
                grid = snapshot.layout["grid"]
                result["samples"].append({"frame": snapshot.frame,"hp":snapshot.hp,
                    "player":snapshot.player.pos.model_dump(),"cells":{
                    i:{k:c[k] for k in ("type","state","collision","variant")}
                    for i,c in grid.items() if c["type"] in (8,9,20)}})
                snapshot = await session.next_snapshot(after=snapshot.snapshot_id, timeout=10)
            result["result"] = "OBSERVED"
        else:
            snapshot = await asyncio.wait_for(session.resolve_environment(snapshot), 120)
            result["final"] = {"frame": snapshot.frame, "clear": snapshot.room["is_clear"],
                               "enemy_count": snapshot.room["enemy_count"], "hp": snapshot.hp,
                               "pos": snapshot.player.pos.model_dump(),
                               "plates": [(i, c) for i, c in snapshot.layout["grid"].items() if c["type"] == 20]}
            result["loss"] = session.task_judge.health.damage
            result["result"] = ("CLEARED" if snapshot.room["is_clear"] and result["loss"] == 0 else
                                "CLEARED_WITH_LOSS" if snapshot.room["is_clear"] else
                                "ENEMY_SPAWNED" if snapshot.blocking_combat else "UNRESOLVED")
            if exit_room and result["result"] == "CLEARED":
                doors = [(int(slot), door) for slot, door in snapshot.layout["doors"].items()
                         if door["is_open"] and not door["is_locked"]]
                if not doors: raise RuntimeError("NO_OPEN_EXIT")
                slot, door = min(doors)
                departed = snapshot.room_visit_id
                arrival, _ = await asyncio.wait_for(
                    session.navigate_door(snapshot, slot, door, snapshot.run_id), 120)
                result["exit"] = {"slot": slot, "left_room": arrival.room_visit_id != departed,
                                  "arrival_room": arrival.room.get("room_variant"),
                                  "hp": arrival.hp}
                if not result["exit"]["left_room"] or arrival.hp != result["initial"]["hp"]:
                    result["result"] = "EXIT_FAILED"
    except Exception as exc:
        result.update(result="FAILED", error=type(exc).__name__ + ": " + str(exc))
    finally:
        try:
            await session.close()
        finally:
            launcher.collect_evidence()
            (directory / "plate-probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
            log.file.close()
            print(json.dumps({k:v for k,v in result.items() if k!="samples"}, ensure_ascii=False), flush=True)
    return 0 if result["result"] in ("CLEARED", "ENEMY_SPAWNED", "OBSERVED") else 1


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observe-only",action="store_true")
    parser.add_argument("--exit",action="store_true",help="Leave through an observed open door after clearing")
    args=parser.parse_args()
    raise SystemExit(asyncio.run(main(args.observe_only,args.exit)))
