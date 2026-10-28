"""Diagnostic J835 Host room control probe; not M3 completion evidence."""
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


async def main():
    directory = ROOT / "artifacts/runs" / datetime.now(timezone.utc).strftime("host114-%Y%m%dT%H%M%S%fZ")
    log = RunLog(directory)
    log.manifest(ROOT, None)
    pause = NativePause(game_executable=ROOT / "The Binding of Isaac Rebirth Repentance/isaac-ng.exe")
    launcher = GameLauncher(ROOT, log, pause, slot=1, timeout=75)
    session = CombatSession(SocketBridge(), None, pause, log, ready_timeout=75,
                            new_run=True, seed="3BYH A82R")
    report = {"source_mode":"diagnostic_initialization","formal_run_proof":False,
              "commands":[],"result":"INCOMPLETE"}
    print("Host probe artifacts: " + str(directory), flush=True)
    try:
        snapshot = await session.open(bootstrap=launcher)
        for command, predicate in (
            ("stage 4", lambda s: s.floor and s.floor.stage == 4 and s.room.get("is_starting_room")),
            ("goto d.114", lambda s: s.room.get("room_variant") == 114),
        ):
            await session.rpc("EXEC_CONSOLE", {"command":command})
            report["commands"].append(command)
            deadline = time.monotonic() + 20
            while not predicate(snapshot):
                if time.monotonic() > deadline: raise TimeoutError("TRANSITION_TIMEOUT: " + command)
                snapshot = await session.next_snapshot(after=snapshot.snapshot_id, timeout=10)
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
            launcher.collect_evidence()
            (directory / "host-probe.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf8")
            log.file.close()
            print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report["result"] == "CLEARED" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
