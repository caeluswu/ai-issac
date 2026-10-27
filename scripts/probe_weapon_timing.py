"""Development-only input/tear probe. Attach to a J835 game; no LLM calls.

Run from the project root. Installs no policy and grants no item support.
The initial clear room is reset, then 69 is granted before measurements.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "SocketBridge/python"))
from facade import SocketBridge
from combat.runtime import CombatSession, RunLog


async def probe(args):
    directory = ROOT / "artifacts/mechanism-probes" / datetime.now(timezone.utc).strftime("charge-%Y%m%dT%H%M%S%fZ")
    log = RunLog(directory)
    log.manifest(ROOT, None)
    session = CombatSession(SocketBridge(), None, None, log, new_run=True, seed=args.seed)
    session.ready_timeout = args.wait
    summary = {"source_mode": "development_initialization", "paid_api_calls": 0,
               "item_id": 69, "seed": args.seed, "stages": [], "success": False,
               "lua_sha256": hashlib.sha256((ROOT/"SocketBridge/main.lua").read_bytes()).hexdigest()}
    print("Probe artifacts: " + str(directory), flush=True)
    try:
        s = await session.open()
        if not s.room.get("is_starting_room") or s.blocking_combat:
            raise RuntimeError("PROBE_REQUIRES_CLEAR_START")
        await session.set_mode("FORCE_AI")

        async def segment(label, shoot, updates, *, send=True):
            nonlocal s
            start = s.frame
            rows = []
            end = time.monotonic() + max(8, updates/15)
            while s.frame-start < updates:
                if time.monotonic() > end: raise TimeoutError("PROBE_UPDATE_TIMEOUT")
                if s.blocking_combat: raise RuntimeError("PROBE_UNEXPECTED_HOSTILE")
                row = {"label":label,"frame":s.frame,"snapshot_id":s.snapshot_id,
                       "room_visit_id":s.room_visit_id,"stats":s.stats,"inventory":s.inventory,
                       "feedback":s.weapon_feedback,"player":s.player.model_dump(),
                       "tears":s.projectiles["player_tears"]}
                rows.append(row); log("probe_frame", **row)
                if send:
                    try: await session.send_action(s, (0,0), shoot, {"probe_stage":label})
                    except TimeoutError as exc:
                        if "STALE_ACTION" not in str(exc): raise
                s = await session.next_snapshot(after=s.snapshot_id)
            return rows

        if not args.pause_only:
            await segment("baseline_hold", (1,0), 25)
            await segment("baseline_release", (0,0), 35)
        log("initialization_command", command="giveitem c69", source_mode="development_initialization")
        await session.rpc("EXEC_CONSOLE", {"command":"giveitem c69"})
        for _ in range(4): s = await session.next_snapshot(after=s.snapshot_id)
        items = s.inventory.get("collectibles") or {}
        if not items.get("69"): raise RuntimeError("ITEM_NOT_OBSERVED")
        for hold in (() if args.pause_only else (3, 10, 20, 35, 50)):
            await segment("settle_"+str(hold), (0,0), 20)
            held = await segment("hold_"+str(hold), (1,0), hold)
            released = await segment("release_"+str(hold), (0,0), 30)
            seen = {str(t.get("instance_id")) for t in held[0]["tears"]}
            new=[]
            for row in held+released:
                for tear in row["tears"]:
                    identity = str(tear.get("instance_id"))
                    if identity not in seen:
                        seen.add(identity); new.append({"observed_frame":row["frame"],**tear})
            feedback={f["frame"]:f for r in held+released for f in (r.get("feedback") or {}).get("history",[])
                      if held[0]["frame"] <= f["frame"] < released[-1]["frame"]}
            complete=[f for f in feedback.values() if f.get("consistent") and all(f.get("queried",{}).get(k) is True for k in ("left","right","up","down"))]
            summary["stages"].append({"hold_updates_requested":hold,"first_frame":held[0]["frame"],
                "release_command_frame":released[0]["frame"],"new_tears":new,
                "complete_feedback_frames":len(complete),"feedback_frames":len(feedback)})
        if not args.pause_only:
            await segment("ttl_hold",(1,0),15)
            ttl=await segment("ttl_no_commands",(0,0),12,send=False)
            summary["ttl"]={"new_tear_ids":sorted({t["instance_id"] for r in ttl for t in r["tears"]}),
                            "final_feedback":ttl[-1]["feedback"]}
        if args.pause_check or args.pause_only:
            (directory/"pause-ready.json").write_text('{"ready":true}',encoding="utf-8")
            print("Waiting for native pause, then resume",flush=True)
            deadline=time.monotonic()+120
            while not (session.heartbeat and session.heartbeat.get("paused")):
                if time.monotonic()>deadline:raise TimeoutError("PAUSE_NOT_OBSERVED")
                try:await session.send_action(s,(0,0),(1,0),{"probe_stage":"before_pause"})
                except (RuntimeError,TimeoutError):pass
                try:s=await session.next_snapshot(after=s.snapshot_id,timeout=.15)
                except TimeoutError:pass
            frozen=session.heartbeat["frame"]
            log("probe_paused",frame=frozen)
            while not (session.observation.fresh() and session.observation.latest.frame>frozen
                       and not session.observation.latest.paused):
                if time.monotonic()>deadline:raise TimeoutError("RESUME_NOT_OBSERVED")
                await asyncio.sleep(.05)
            s=session.observation.latest
            resumed=await segment("after_pause",(0,0),15)
            summary["pause"]={"frozen_frame":frozen,"resume_frame":resumed[0]["frame"],
                              "tears":{t["instance_id"]:t for r in resumed for t in r["tears"]}}
        summary["success"] = (bool(summary["stages"]) and all(x["new_tears"] for x in summary["stages"])) or (args.pause_only and bool(summary.get("pause")))
    except BaseException as exc:
        summary["error"] = type(exc).__name__+": "+str(exc)
        log("probe_fault",error=summary["error"])
    finally:
        try:await session.close()
        except Exception as exc:
            summary["cleanup_error"]=type(exc).__name__+": "+str(exc)
            summary["success"]=False
        (directory/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
        log("probe_summary",**summary)
        log.file.close()
    print(json.dumps(summary,ensure_ascii=True),flush=True)
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed",default="3BYH A82R")
    parser.add_argument("--wait",type=float,default=120)
    parser.add_argument("--pause-check",action="store_true",help="wait for externally operated native pause/resume")
    parser.add_argument("--pause-only",action="store_true",help="only test native pause/resume after granting the item")
    raise SystemExit(asyncio.run(probe(parser.parse_args())))
