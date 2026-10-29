"""Diagnostic J835 enemy room control probe; not M3 completion evidence."""
import asyncio
import argparse
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


async def main(stage="4", room=114, exact_fixture=False, current=False, retain_build=False,items=()):
    directory = ROOT / "artifacts/runs" / datetime.now(timezone.utc).strftime("enemy-room-%Y%m%dT%H%M%S%fZ")
    log = RunLog(directory)
    log.manifest(ROOT, None)
    pause = NativePause(game_executable=ROOT / "The Binding of Isaac Rebirth Repentance/isaac-ng.exe")
    launcher = GameLauncher(ROOT, log, pause, slot=1, timeout=75)
    session = CombatSession(SocketBridge(), None, pause, log, ready_timeout=75,
                            new_run=not (current or retain_build), seed=None if current or retain_build else "3BYH A82R")
    report = {"source_mode":"diagnostic_continuation" if current else "diagnostic_initialization","formal_run_proof":False,
              "commands":[],"result":"INCOMPLETE"}
    print("Enemy probe artifacts: " + str(directory), flush=True)
    try:
        snapshot = await session.open(bootstrap=None if current or retain_build else launcher)
        if items and (current or retain_build):raise ValueError('ITEMS_REQUIRE_FRESH_DIAGNOSTIC')
        for item in items:
            before=snapshot.inventory['collectibles'].get(str(item),0)
            command='giveitem c'+str(item)
            await session.rpc('EXEC_CONSOLE',{'command':command});report['commands'].append(command)
            deadline=time.monotonic()+5
            while snapshot.inventory['collectibles'].get(str(item),0)<=before:
                if time.monotonic()>deadline:raise TimeoutError('DIAGNOSTIC_ITEM_NOT_CONFIRMED')
                snapshot=await session.next_snapshot(after=snapshot.snapshot_id)
        if current and (snapshot.room.get('room_variant') != room or exact_fixture):
            raise ValueError('CURRENT_COMBAT_ROOM_MISMATCH')
        if retain_build and (not snapshot.alive or snapshot.blocking_combat or exact_fixture):
            raise ValueError('RETAIN_BUILD_REQUIRES_CLEAR_ROOM')
        commands = (("goto d."+str(room), lambda s: s.room.get("room_variant") == room and not s.room['is_clear']),) if retain_build else () if current else (
            ("stage "+stage, lambda s: s.floor and s.floor.stage == int(stage[0]) and s.room.get("is_starting_room")),
            ("goto d."+str(room), lambda s: s.room.get("room_variant") == room),
        )
        for command, predicate in commands:
            await session.rpc("EXEC_CONSOLE", {"command":command})
            report["commands"].append(command)
            deadline = time.monotonic() + 20
            while not predicate(snapshot):
                if time.monotonic() > deadline: raise TimeoutError("TRANSITION_TIMEOUT: " + command)
                snapshot = await session.next_snapshot(after=snapshot.snapshot_id, timeout=10)
        if exact_fixture:
            initialized = await session.rpc("PREPARE_HOST_PROBE", {"confirm":"diagnostic_initialization"})
            if not initialized.get("success"): raise RuntimeError(str(initialized))
            report["fixture"] = initialized
            for _ in range(3): snapshot = await session.next_snapshot(after=snapshot.snapshot_id)
        for _ in range(3): snapshot = await session.next_snapshot(after=snapshot.snapshot_id)
        if not snapshot.blocking_combat: raise RuntimeError("EMPTY_ENEMY_FIXTURE")
        report["initial"] = {"frame":snapshot.frame,"hp":snapshot.hp,
                             "room_variant":snapshot.room["room_variant"],
                             "enemies":[{"type":e.type,"variant":e.variant,"state":e.state,
                                         "vulnerable":e.vulnerable,"hp":e.hp}
                                        for e in snapshot.hostiles]}
        result = await asyncio.wait_for(session.execute_room(snapshot), 120)
        final = session.observation.latest
        row = session.room_executor.results[-1]
        report["final"] = {"frame":final.frame,"hp":final.hp,
                           "clear":final.room["is_clear"],"result":result,
                           "ticks":row["ticks"],"damage":row["damage"]}
        report["result"] = "CLEARED" if final.room["is_clear"] and result == "CLEARED" else result
    except Exception as exc:
        report.update(result="FAILED", error=type(exc).__name__ + ": " + str(exc))
    finally:
        try: await session.close()
        finally:
            if not current and not retain_build: launcher.collect_evidence()
            (directory / "enemy-probe.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf8")
            log.file.close()
            print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report["result"] == "CLEARED" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage",choices=("1","1a","2","2a","4","4a","5","5a","6","6a"),default="4")
    parser.add_argument("--room",type=int,default=114)
    parser.add_argument("--exact-fixture",action="store_true")
    parser.add_argument("--current",action="store_true",help="Diagnose the matching current combat room without restarting or console initialization")
    parser.add_argument('--retain-build',action='store_true',help='Initialize only a diagnostic room with the current earned build')
    parser.add_argument('--items',type=int,nargs='*',default=(),help='Explicit diagnostic loadout before measurement')
    args = parser.parse_args()
    if args.current and args.retain_build:parser.error('choose current or retain-build')
    if any(item<=0 or item>732 for item in args.items):parser.error('unsupported diagnostic item ID')
    raise SystemExit(asyncio.run(main(args.stage,args.room,args.exact_fixture,args.current,args.retain_build,args.items)))
