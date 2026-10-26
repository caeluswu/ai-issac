"""Observe V2 in an already started game; no movement, restart or paid API."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"SocketBridge/python"))
from facade import SocketBridge
from combat.observation import SnapshotAssembler


async def run(args):
    output = args.output_dir or ROOT/"artifacts/runs"/datetime.now(timezone.utc).strftime("m2-observation-%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, exist_ok=False)
    bridge, assembler = SocketBridge(), SnapshotAssembler()
    frames, errors = [], []
    source = ROOT/"SocketBridge/main.lua"
    installed = ROOT/"The Binding of Isaac Rebirth Repentance/mods/SocketBridge/main.lua"
    hashes = {"source": hashlib.sha256(source.read_bytes()).hexdigest(),
              "installed": hashlib.sha256(installed.read_bytes()).hexdigest() if installed.exists() else None}
    print("Observation artifacts: "+str(output), flush=True)
    with (output/"trace.jsonl").open("w", encoding="utf-8") as trace:
        def receive(raw):
            s = assembler.consume(raw)
            if assembler.error:
                if assembler.error not in errors: errors.append(assembler.error)
            if s is not None:
                trace.write(json.dumps(s.model_dump(), ensure_ascii=False)+"\n")
                trace.flush()
                if s.schema_version == 2: frames.append(s)
        bridge.on_message(receive)
        started = time.monotonic()
        try:
            if hashes["source"] != hashes["installed"]: raise RuntimeError("INSTALLED_MOD_MISMATCH")
            await bridge.start()
            while not bridge.connected:
                if time.monotonic()-started > args.timeout: raise TimeoutError("GAME_CONNECTION_TIMEOUT")
                await asyncio.sleep(.1)
            await bridge.set_control_mode("MANUAL")
            await bridge.subscribe(["COMBAT_FRAME"], {"COMBAT_FRAME": {"enabled": True}})
            await bridge.send_command("GET_FULL_STATE")
            while len(frames) < args.samples:
                if time.monotonic()-started > args.timeout: raise TimeoutError("V2_OBSERVATION_TIMEOUT")
                if not bridge.connected: raise RuntimeError("GAME_DISCONNECTED")
                await asyncio.sleep(.05)
            s = frames[-1]
            if any(x.weapons.status != "known" or x.effects.status != "known" or
                   x.inventory_coverage.static_status != "known" for x in frames):
                raise RuntimeError("REQUIRED_OBSERVATION_PARTIAL")
            summary = {"result": "OBSERVATION_V2_READY", "samples": len(frames),
                       "frame_range": [frames[0].frame, s.frame], "mode": s.mode,
                       "floor": s.floor.model_dump(), "weapons": s.weapons.model_dump(),
                       "effects": s.effects.model_dump(), "inventory_coverage": s.inventory_coverage.model_dump(),
                       "health": s.health_observation.model_dump(), "pickups_seen": sum(len(x.pickups) for x in frames),
                       "source_hashes": hashes, "validation_errors": errors,
                       "scope": "live_observation_only_not_item_pickup_or_combat", "paid_api_calls": 0}
            if errors: raise RuntimeError("SNAPSHOT_VALIDATION_ERRORS")
        except Exception as exc:
            summary = {"result": "FAILED", "error": type(exc).__name__+": "+str(exc),
                       "samples": len(frames), "source_hashes": hashes, "validation_errors": errors}
        finally:
            if bridge.connected: await bridge.set_control_mode("MANUAL")
            await bridge.stop()
    (output/"summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["result"] == "OBSERVATION_V2_READY" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--samples", type=int, default=15)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.timeout <= 0 or not 2 <= args.samples <= 300: parser.error("invalid timeout or sample count")
    raise SystemExit(asyncio.run(run(args)))
